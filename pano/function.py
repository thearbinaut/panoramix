# coding: tilde

from utils.helpers import COLOR_HEADER, COLOR_BLUE, COLOR_OKGREEN, COLOR_WARNING, FAIL
from utils.helpers import ENDC, COLOR_BOLD, COLOR_UNDERLINE, COLOR_GREEN, COLOR_GRAY

from utils.helpers import color, C#.header, blue, okgreen, warning, red, bold, underline, green, gray, endc

from utils.helpers import EasyCopy, opcode, find_f_list, find_f, replace_f

from pano.prettify import prettify, pprint_logic

from core.arithmetic import simplify_bool

import collections

from copy import deepcopy

from utils.signatures import get_func_name, set_func, get_abi_name, get_func_params, set_func_params_if_none

import pano.folder
from core.masks import mask_to_type, type_to_mask

import json

import logging
logger = logging.getLogger(__name__)

def find_parents(exp, child):
    if type(exp) not in (list, tuple):
        return []

    res = []

    for e in exp:
        if e == child:
            res.append(exp)
        res.extend(find_parents(e, child))

    return res


class Function(EasyCopy):

    def __init__(self, hash, trace):
        self.hash = hash
        self.name = get_func_name(hash)
        self.color_name = get_func_name(hash, add_color=True)
        self.abi_name = get_abi_name(hash)

        self.const = None
        self.read_only = None
        self.payable = None

        self.hash = hash

        self.trace = deepcopy(trace)
        self.orig_trace = deepcopy(self.trace)

        self.params = self.make_params()

        if 'unknown' in self.name:
            self.make_names()

        self.trace = self.cleanup_masks(self.trace)
        self.ast = None

        self.analyse()

        assert self.payable is not None

        self.is_regular = self.const is None and \
                          self.getter is None

    def cleanup_masks(self, trace):
        def rem_masks(exp):
            if exp ~ ('bool', ('cd', int:idx)):
                if idx in self.params and \
                   self.params[idx][0] == 'bool':
                        return ('cd', idx)

            elif exp ~ ('mask_shl', :size, 0, 0, ('cd', int:idx)):
                if idx in self.params:
                    kind = self.params[idx][0]
                    def_size = type_to_mask(kind)
                    if size == def_size:
                        return ('cd', idx)

            return exp

        return replace_f(trace, rem_masks)

    def make_names(self):
        new_name = self.name.split('(')[0]

        self.name = '{}({})'.format(new_name,
                                    ', '.join((p[0]+' '+p[1]) for p in self.params.values()))
        self.color_name = '{}({})'.format(new_name,
                                    ', '.join((p[0]+' '+COLOR_GREEN+p[1]+ENDC) for p in self.params.values()))


        self.abi_name = '{}({})'.format(new_name,
                                    ','.join(p[0] for p in self.params.values()))


    def ast_length(self):
        if self.trace is not None:
            return len((self.print().split('\n'))), len(self.print())
        else:
            return 0, 0

    def priority(self):
        # sorts functions in this order:
        # - self-destructs
        # - (read-only? would be nice, but some read-only funcs can be very long, e.g. etherdelta)
        # - length

        if self.trace is None:
            return 0

        if 'selfdestruct' in str(self.trace):
            return -1

        else:
            return self.ast_length()[1]

    def make_params(self):
        '''
            figures out parameter types from the decompiled function code.

            does so by looking at all 'cd'/calldata occurences and figuring out
            how they are accessed - are they masked? are they used as pointers?

        '''

        params = get_func_params(self.hash)
        if len(params) > 0:
            res = {}
            idx = 4
            for p in params:
                res[idx] = (p['type'], p['name'])
                idx += 32
        else:
            # good testing: solidstamp, auditContract
            # try to find all the references to parameters and guess their types
            occurences = find_f_list(self.trace, lambda exp: [exp] if (exp ~ ('mask_shl',_,_,_,('cd',_)) or exp ~ ('cd',_)) else [])
            sizes = {}
            for o in occurences:
                o ~ ('mask_shl', :size, _, _, ('cd', :idx))

                if o ~ ('cd', :idx):
                    size = 256

                if idx == 0:
                    continue

                if idx ~ ('add', 4, ('cd', :in_idx)):
                    # this is a mark of 'cd' being used as a pointer
                    sizes[in_idx] = -1
                    continue

                if idx not in sizes: 
                    sizes[idx] = size
                    
                elif size < sizes[idx]:
                    sizes[idx] == size


            for idx in sizes:
                if type(idx) != int or (idx-4) % 32 != 0:
                    logger.warning('unusual cd')
                    return {}

            # for every idx check if it's a bool by any chance
            for idx in sizes:
                li = find_parents(self.trace, ('cd', idx))
                for e in li:
                    if opcode(e) not in ('bool', 'if', 'iszero'):
                        break

                    if e ~ ('mask_shl', _, :off, _, :val):
                        assert val == ('cd', idx)
                        if off != 0:
                            sizes[idx] = -2 # it's a tuple!
                else:
                    sizes[idx] = 1

            res = {}
            count = 1
            for k in sizes:

                if type(k) != int:
                    logger.warning(f'unusual calldata reference {k}')
                    return {}

            for idx in sorted(sizes.keys()):
                size = sizes[idx]

                if size == -2:
                    kind = 'tuple'
                elif size == -1:
                    kind = 'array'
                elif size == 1:
                    kind = 'bool'
                else:
                    kind = mask_to_type(size, force=True )

                assert kind != None, size

                res[idx] = (kind, f'_param{count}')
                count += 1

        return res

    def serialize(self):
        trace = self.trace

        res = {
            'hash': self.hash,
            'name': self.name,
            'color_name': self.color_name,
            'abi_name': self.abi_name,
            'length': self.ast_length(),
            'getter': self.getter,
            'const': self.const,
            'payable': self.payable,
            'print': self.print(),
            'trace': trace,
            'params': self.params,
        }
        try:
            assert json.dumps(res) # check if serialisation works well
        except:        
            logger.error('failed serialization %s', self.name)
            raise

        return res


    def print(self):
        out = self._print()
        return "\n".join(out)

    def _print(self):
        set_func(self.hash)
        set_func_params_if_none(self.params)

        if self.const is not None:

            val = self.const
            val ~ ('return', :val)

            return [COLOR_HEADER+"const "+ENDC+str(self.color_name.split('()')[0])+" = "+COLOR_BOLD+prettify(val)+ENDC]

        else:
            comment = ""

            if not self.payable:
                comment = "# not payable"

            if self.name == "_fallback()":
                if self.payable:
                    comment = "# default function"
                else:
                    comment = "# not payable, default function"  # qweqw

            header = [ color("def ", C.header) + self.color_name +
                       (color(" payable", C.header) if self.payable else "") + ': ' +
                       color(comment, C.gray) ]

            if self.ast is not None:
                res = list(pprint_logic(self.ast))
            else:
                res = list(pprint_logic(self.trace))

            if len(res) == 0:
                res = ['  stop']

            return header + res

    def simplify_string_getter_from_storage(self):
        ''' 
            a heuristic for finding string getters and replacing them
            with a simplified version

            test cases: unicorn
                        0xF7dF66B1D0203d362D7a3afBFd6728695Ae22619 name
                        0xf8e386EDa857484f5a12e4B5DAa9984E06E73705 version

            if you want to see how it works, turn this func off
            and see how test cases decompile
        '''

        if not self.read_only:
            return

        if len(self.returns) == 0:
            return

        for r in self.returns:
            test = r ~ ('return', ('data', ('arr', ('storage', 256, 0, ('length', :loc)), ...)))

            if not test:
                return

        self.trace = [('return', ('storage', 256, 0, ('array', ('range', 0, ('storage', 256, 0, ('length', loc))), loc)))]
        self.getter = self.trace[0][1]


    def analyse(self):
        assert len(self.trace) > 0

        def find_returns(exp):
            if opcode(exp) == 'return':
                return [exp]
            else:
                return []

        self.returns = find_f_list(self.trace, find_returns)

        first = self.trace[0]

        if opcode(first) == 'if' and simplify_bool(first[1]) == 'callvalue'   \
                and (first[2][0] == ('revert', 0) or opcode(first[2][0]) == 'invalid'):
            self.trace = self.trace[0][3]
            self.payable = False
        elif opcode(first) == 'if' and simplify_bool(first[1]) == ('iszero', 'callvalue')   \
                and (first[3][0] == ('revert', 0) or opcode(first[3][0]) == 'invalid'):
            self.trace = self.trace[0][2]
            self.payable = False
        else:
            self.payable = True

        self.read_only = True
        for op in ['store', 'selfdestruct', 'call', 'delegatecall', 'codecall', 'create']:
            if f"'{op}'" in str(self.trace):
                self.read_only = False


        '''
            const func detection
        '''

        self.const = self.read_only
        for exp in ['storage', 'calldata', 'calldataload', 'store', 'cd']:
            if exp in str(self.trace) or len(self.returns)!=1:
                self.const = False

        if self.const:
            self.const = self.returns[0]
            if len(self.const) == 3 and opcode(self.const[2]) == 'data':
                self.const = self.const[2]
            if len(self.const) == 3 and opcode(self.const[2]) == 'mask_shl':
                self.const = self.const[2]
            if len(self.const) == 3 and type(self.const[2]) == int:
                self.const = self.const[2]
        else:
            self.const = None

        '''
            getter detection
        '''

        self.getter = None
        self.simplify_string_getter_from_storage()
        if self.const is None and \
           self.read_only and \
           len(self.returns) == 1:
                ret = self.returns[0][1]
                if ret ~ ('bool', ('storage', _, _, :loc)):
                    self.getter = ret  # we have to be careful when using this for naming purposes,
                                       # because sometimes the storage can refer to array length

                elif opcode(ret) == 'mask_shl' and opcode(ret[4]) == 'storage':
                    self.getter = ret[4]
                elif opcode(ret) == 'storage':
                    self.getter = ret
                elif ret ~ ('data', *terms):
                    # for structs, we check if all the parts of the struct are storage from the same
                    # location. if so, we return the location number

                    t0 = terms[0]  # 0xFAFfea71A6da719D6CAfCF7F52eA04Eb643F6De2 - documents
                    if t0 ~ ('storage', 256, 0, :loc):
                        for e in terms[1:]:
                            if not (e ~ ('storage', 256, 0, ('add', _, loc))):
                                break
                        else:
                            self.getter = t0

                    # kitties getKitten - with more cases this and the above could be uniformed
                    if self.getter is None:
                        prev_loc = -1
                        for e in terms:
                            def l2(x):
                                if x ~ ('sha3', ('data', _, int:l)):
                                    if l < 1000:
                                        return l
                                if x ~ ('sha3', int:l) and l < 1000:
                                    return l
                                return None

                            loc = find_f(e, l2)
                            if not loc or (prev_loc != -1 and prev_loc != loc):
                                break
                            prev_loc = loc

                        else:
                            self.getter = ('struct', ('loc', loc))

                else:
                    pass

        return self
