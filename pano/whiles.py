# coding: tilde

from copy import copy
import core.arithmetic as arithmetic
import logging
import collections

from core.memloc import range_overlaps, splits_mem, fill_mem, memloc_overwrite, split_setmem, apply_mask_to_range, split_store

from utils.helpers import rewrite_trace_multiline, opcode, cached, walk_trace, to_exp2, replace, find_op_list
from utils.helpers import contains, find_f_set, find_f_list, rewrite_trace, rewrite_trace_full, replace, replace_f, replace_f_stop, rewrite_trace_ifs

from core.algebra import simplify, calc_max, add_ge_zero, minus_op, sub_op, flatten_adds, max_to_add, divisible_bytes, _max_op, div_op
from core.algebra import add_op, bits, mul_op, get_sign, safe_ge_zero, ge_zero, lt_op, safe_lt_op, safe_le_op, simplify_max, le_op, max_op, safe_max_op, safe_min_op, min_op, or_op, neg_mask_op, mask_op, apply_mask_to_storage, apply_mask, try_add, to_bytes

from core.arithmetic import is_zero, to_real_int

from pano.prettify import pformat_trace, pprint_trace, pprint_repr

from .postprocess import cleanup_mul_1

from utils.profiler import checkpoint, checkpoint_start, log_checkpoints, func_caller

from core.masks import get_bit

from core.masks import to_mask, to_neg_mask

from .rewriter import simplify_exp, postprocess_exp, postprocess_trace, rewrite_string_stores

from pano.prettify import pretty_repr

import sys

logger = logging.getLogger(__name__)
logger.level = logging.CRITICAL # switch to INFO for detailed

'''

    Rube Goldberg would be proud.

'''

def make_whiles(trace): 

    logger.info('making')
    trace = make(trace)
    logger.info('cleaning up jumpdests')

    # clean up jumpdests
    trace = rewrite_trace(trace, lambda line: [] if line ~ ('jumpdest', ...) else [line])

    old_trace = None
    count = 0
    while trace != old_trace:
        count += 1
        if count > 40:
            break

        old_trace = trace
        trace = replace_f(trace, simplify_exp)
        trace = cleanup_vars(trace)
        trace = cleanup_mems(trace)
        trace = rewrite_trace(trace, split_setmem)
        trace = rewrite_trace_full(trace, split_store)
        trace = cleanup_vars(trace)
        trace = replace_f(trace, simplify_exp)
        trace = cleanup_mul_1(trace)
        trace = cleanup_msize(trace)
        trace = replace_bytes_or_string_length(trace)
        trace = cleanup_conds(trace)
        trace = rewrite_trace(trace, loop_to_setmem)
        trace = propagate_storage_in_loops(trace)
    
    # final lightweight postprocessing
    # introduces new variables, simplifies code for human readability
    # and does other stuff that would break the above loop

    trace = replace_f(trace, max_to_add)

    trace = replace_f(trace, postprocess_exp)
    trace = replace_f(trace, postprocess_exp)

    trace = rewrite_trace_ifs(trace, postprocess_trace)

    trace = rewrite_trace_multiline(trace, rewrite_string_stores, 3)
    trace = cleanup_mems(trace)
    trace = cleanup_mems(trace)
    trace = cleanup_mems(trace)
    trace = cleanup_conds(trace)

    def fix_storages(exp):
        if exp ~ ('storage', :size, int:off, :loc) and off < 0:
            return ('storage', size, 0, loc)
        return exp

    trace = replace_f(trace, fix_storages)
    trace = cleanup_conds(trace)

    logger.debug('readability')
    trace = readability(trace)

    trace = cleanup_mul_1(trace)

    return trace


def replace_while_var(rest, counter_idx, new_idx):
    while contains(rest, ('var', new_idx)):
        new_idx += 1

    def r(exp):
        if exp == ('var', counter_idx):
            return ('var', new_idx)

        elif exp ~ ('setvar', counter_idx, :val):
            return ('setvar', new_idx, val)

        else:
            return exp

    return simplify_exp(replace_f(rest, r)), new_idx


def canonise_max(exp):
    if opcode(exp) == 'max':
        args = []
        for e in exp[1:]:

            if e ~ ('mul', 1, :num):
                args.append(num)
            else:
                args.append(e)

        args.sort(key=lambda x: str(x) if type(x) != int else ' ' + str(x))
        return ('max', ) + tuple(args)
    else:
        return exp


assert canonise_max(('max', ('mul', 1, ('x','y')), 4)) == ('max', 4, ('x', 'y'))


def readability(trace):
    '''
        - replaces variable names with nicer ones,
        - fixes empty memory in calls
        - replaces 'max..' in setmems with msize variable
            (max can only appear because of this)
    '''

    trace = replace_f(trace, canonise_max)

    res = []
    for idx, line in enumerate(trace):

        if line ~ ('setmem', ('range', ('add', *add_params), _), :mem_val):
            for m in add_params:
                if m ~ ('max', ...):
                    res.append(('setvar','_msize', m))

                    def x(line):
                        return [replace(line, m, ('var','_msize'))]

                    rest = rewrite_trace(trace[idx:], x)
                    res.extend(readability(rest))
                    return res

        elif line ~ ('if', :cond, :if_true, :if_false):

            # if if_false ~ [('revert', ...)]: # no lists in Tilde... yet :,)
            if len(if_false) == 1 and opcode(if_false[0]) == 'revert':
                res.append(('if', is_zero(cond), readability(if_false), readability(if_true))) 
            else:
                res.append(('if', cond, readability(if_true), readability(if_false)))
            continue

        elif line ~ ('while', ...):
            # for whiles, normalize variable names

            a = parse_counters(line)

            rest = trace[idx:]

            if 'counter' in a:
                counter_idx = a['counter']
                rest, _ = replace_while_var(rest, counter_idx, 0)

            else:
                counter_idx = -1

            new_idx = 1

            cond, path, jds, vars = line[1:]

            for _, v_idx, _ in vars:
                if v_idx != counter_idx:
                    rest, new_idx = replace_while_var(rest, v_idx, new_idx)

            line, rest = rest[0], rest[1:]
            cond, path, jds, vars = line[1:]

            path = readability(path)
            res.append(('while', cond, path, jds, vars))

            res.extend(readability(rest))
            return res


        res.append(line)

    return res


def replace_bytes_or_string_length(trace):
    def replace(expr):
        key = None
        expr ~ ('mask_shl', :size, :offset, -1, ('and', ('storage', _, 0, :key), ('add', -1, ('mask_shl', _, _, _, ('iszero', ('storage', _, 0, :key2))))))
        expr ~ ('mask_shl', :size, :offset, -1, ('and', ('add', -1, ('mask_shl', _, _, _, ('iszero', ('storage', _, 0, :key2)))), ('storage', _, 0, :key)))
        if key is None or key != key2:
            return

        if type(key) == int:
            key = ('loc', key)

        if size == 255 and offset == 1:
            return ('storage', 256, 0, ('length', key))
        assert offset >= 1
        return ('mask_shl', size, offset - 1, 0, ('storage', 256, 0, ('length', key)))

    return replace_f_stop(trace, replace)


def loop_to_setmem(line):

    if line ~ ('while', ...):
        r = _loop_to_setmem(line)

        if r is not None:
            return r

        r = loop_to_setmem_from_storage(line)

        if r is not None:
            return r

    return [line]


def vars_in_expr(expr):
    if expr ~ ('var', :var_id):
        return frozenset([var_id])

    s = frozenset()

    if type(expr) not in (tuple, list):
        return s

    for e in expr:
        s = s | vars_in_expr(e)
    return s

def only_add_in_expr(op):
    if op ~ ('setvar', :idx, :val):
        return only_add_in_expr(val)
    if op ~ ('add', *terms):
        return all(only_add_in_expr(o) for o in terms)
    if op ~ ('var', _):
        return True
    if op ~ ('sha3', :term):  # sha3(constant) is allowed.
        return opcode(term) is None
    if opcode(op) is not None:
        return False
    return True

assert only_add_in_expr(('setvar', 100, ('mul', ('var', 100), 1))) is False
assert only_add_in_expr(('setvar', 100, ('add', ('var', 100), 1))) is True

def propagate_storage_in_loop(line):

    assert line ~ ('while', :cond, :path, :jds, :setvars)

    def storage_sha3(value):
        if value ~ ('add', *terms):
            for op in terms:
                if storage_sha3(op) is not None:
                    return storage_sha3(op)

        if value ~ ('sha3', :val):
            if type(val) != int or val < 1000:  # used to be int:val here, why?
                return value

    def path_only_add_in_continue(path):
        for op in path:
            if opcode(op) == 'continue':
                _, _, instrs = op
                if any(not only_add_in_expr(instr) for instr in instrs):
                    return False
        return True

    new_setvars = []

    for setvar in setvars:
        assert setvar ~ ('setvar', :var_id, :value)

        sha3 = storage_sha3(value)
        if not sha3:
            new_setvars.append(setvar)
            continue

        # If the "continue" instructions don't only add stuff to the index variable,
        # it's not safe to proceed. If we would do "i = i * 2", then it doesn't
        # make sense to substract a constant to "i".
        if not path_only_add_in_continue(path):
            new_setvars.append(setvar)
            continue

        new_setvars.append(('setvar', var_id, sub_op(value, sha3)))

        def add_sha3(t):
            # We replace occurrences of var by "var + sha3"
            if t == ('var', var_id):
                return add_op(t, sha3)
            # Important: for "continue" we don't want to touch the variable.
            # TODO: This is only valid if the "continue" contains only
            # operators like "+" or "-". We should check that.
            if opcode(t) == 'continue':
                return t

        path = replace_f_stop(path, add_sha3)
        cond = replace_f_stop(cond, add_sha3)

    return [('while', cond, path, jds, new_setvars)]


def propagate_storage_in_loops(trace):
    def touch(line):
        if line ~ ('while', ...):

            r = propagate_storage_in_loop(line)
            if r is not None:
                return r

        return [line]

    return rewrite_trace(trace, touch)


def _loop_to_setmem(line):
    def memidx_to_memrange(mem_idx, setvars, stepvars, endvars):
        mem_idx_next = mem_idx
        for v in stepvars:
            assert v ~ ('setvar', :v_idx, :v_val)
            mem_idx_next = replace(mem_idx_next, ('var', v_idx), v_val)

        diff = sub_op(mem_idx_next, mem_idx)

        if diff not in (32, -32):
            return None, None

        mem_idx_last = mem_idx
        for v_idx, v_val in endvars.items():

            mem_idx_last = replace(mem_idx_last, ('var', v_idx), v_val)

        mem_idx_first = mem_idx
        for v in setvars:
            assert v ~ ('setvar', :v_idx, :v_val)

            mem_idx_first = replace(mem_idx_first, ('var', v_idx), v_val)

        if diff == 32:
            mem_len = sub_op(mem_idx_last, mem_idx_first)

            return ('range', mem_idx_first, mem_len), diff

        else:
            assert diff == -32

            mem_idx_last = add_op(32, mem_idx_last)
            mem_idx_first = add_op(32, mem_idx_first)

            mem_len = sub_op(mem_idx_first, mem_idx_last)

            return ('range', mem_idx_lasst, mem_len), diff

    assert line ~ ('while', :cond, :path, :jds, :setvars)

    if len(path) != 2:
        return None

    if opcode(path[1]) != 'continue':
        return None

    if opcode(path[0]) != 'setmem':
        return None

    setmem = path[0]
    cont = path[1]

    mem_idx, mem_val = setmem[1], setmem[2]

    assert mem_idx ~ ('range', :i, :l)
    if l != 32:
        return None
    mem_idx = i

    stepvars = cont[2]

    a = parse_counters(line)
    if 'endvars' not in a:
        return None

    setvars, endvars = a['setvars'], a['endvars']

    rng, diff = memidx_to_memrange(mem_idx, setvars, stepvars, endvars)

    if rng is None:
        return None

    if mem_val == 0:
        res = [('setmem', rng, 0)]

    elif opcode(mem_val) == 'mem':
        mem_val_idx = mem_val[1]
        assert mem_val_idx ~ ('range', :i, :l)
        mem_val_idx = i
        if l != 32:
            return None

        val_rng, val_diff = memidx_to_memrange(mem_val_idx, setvars, stepvars, endvars)
        
        if val_rng is None:
            return None
        
        if val_diff != diff:
            return None # possible but unsupported

        # we should check for overwrites here, but skipping for now
        # if the part of memcopy loop overwrites source before it's copied,
        # we can end up with unexpected behaviour, could at least show some warning,
        # or set that mem to 'complicated' or sth

        res = [('setmem', rng, ('mem', val_rng))]

    else:
        return None

    for v_idx, v_val in endvars.items():
        res.append(('setvar', v_idx, v_val))

    return res

def loop_to_setmem_from_storage(line):
    assert opcode(line) == 'while'
    cond, path, jds, setvars = line[1:]

    if len(path) != 2 \
       or opcode(path[0]) != 'setmem' \
       or opcode(path[1]) != 'continue':
        return None

    setmem = path[0]
    cont = path[1]

    logger.debug("loop_to setmem_from_storage: %s\n%s\n%s", setmem, cont, cond)

    # (setmem, mem_idx, mem_val)
    mem_idx, mem_val = setmem[1], setmem[2]

    # Extract the interesting variable from mem_idx
    vars_in_idx = vars_in_expr(mem_idx)
    if len(vars_in_idx) != 1:
        return
    if not only_add_in_expr(mem_idx):
        return
    memory_index_var = next(iter(vars_in_idx))

    # Same from mem_val
    vars_in_val = vars_in_expr(mem_val)
    if len(vars_in_val) != 1:
        return
    storage_key_var = next(iter(vars_in_val))
    if opcode(mem_val) != 'storage':
        return
    if mem_val[1] != 256 or mem_val[2] != 0:
        return
    storage_key = mem_val[3]
    if not only_add_in_expr(storage_key):
        return

    logger.debug("now look at the continue")
    update_memory_index = ('setvar', memory_index_var, ('add', 32, ('var', memory_index_var)))
    update_storage_key = ('setvar', storage_key_var, ('add', 1, ('var', storage_key_var)))
    if set(cont[2]) != {update_memory_index, update_storage_key}:
        return

    logger.debug("setvars")
    memory_index_start = None
    storage_key_start = None
    memory_index_init = None
    for setvar in setvars:
        if setvar[1] == memory_index_var:
            memory_index_start = replace(mem_idx, ('var', memory_index_var), setvar[2])
            memory_index_init = setvar[2]
        elif setvar[1] == storage_key_var:
            storage_key_start = replace(storage_key, ('var', storage_key_var), setvar[2])
        else:
            return


    logger.debug("while condition")
    if memory_index_var not in vars_in_expr(cond):
        return
    if opcode(cond) != 'gt':
        return

    mem_count = ('add', cond[1], ('mul', -1, cond[2]))
    mem_count = replace(mem_count, ('var', memory_index_var), memory_index_init)

    mem_rng = ('range', memory_index_start, mem_count)
    storage_rng = ('range', storage_key_start, ('div', mem_count, 32))

    logger.debug("mem_rng: %s, storage_rng: %s", mem_rng, storage_rng)
    return [('setmem', mem_rng, ('storage', 256, 0, storage_rng))]


'''

    simplifier

'''

def apply_constraint(exp, constr):
    # for constraints like "isZero XX % 32", applies them to expression

    return exp

    if constr ~ ('mask_shl', 5, 0, 0, :val):
        def f(x):
            if x ~ ('mask_shl', int:size, 5, int:shl, ('add', 31, :val)):
                return ('add', 32 * (2**shl), ('mask_shl', size, 5, shl, val))
            if x ~ ('mask_shl', int:size, 5, 0, :val):
                return ('mask_shl', size, 5, 0, val)

            return x

        return replace_f(exp, f)


    if constr ~ ('iszero', ('mask_shl', 5, 0, 0, :val)):
        def f(x):
            if x ~ ('mask_shl', int:size, 5, 0, ('add', 31, :val)):
                return ('mask_shl', size+5, 0, 0, val)
            if x ~ ('mask_shl', int:size, 5, 0, :val):
                return ('mask_shl', size+5, 0, 0, val)
            if x ~ ('mask_shl', 5, 0, 0, :val):
                return 0

            return x

        return replace_f(exp, f)

    return exp


def cleanup_conds(trace):
    '''

        removes ifs/whiles with conditions that are obviously true
        and replace variables that need to be equal to a constant by that constant

    '''

    res = []

    for line in trace:
        if line ~ ('while', :cond, :path, :jds, :setvars):
            # we're not evaluating symbolically, otherwise stuff like
            # stor0 <= stor0 + 1 gets evaluated to `True` - this happens
            # because we're truncating mask256. it should really be
            # mask(256, stor0) <= mask(256, stor0 + 1)
            # which is not always true
            # see 0x014B50466590340D41307Cc54DCee990c8D58aa8.transferFrom 
            path = cleanup_conds(path)
            ev = arithmetic.eval_bool(cond, symbolic=False)
            if ev is True:
                res.append(('while', ('bool', 1), path, jds, setvars))
            elif ev is False:
                pass # removing loop altogether
            else:
                res.append(('while', cond, path, jds, setvars))

        elif line ~ ('if', :cond, :if_true, :if_false):
            if_true = cleanup_conds(if_true)
            if_false = cleanup_conds(if_false)

            # If the condition is always true/false, remove the if.
            ev = arithmetic.eval_bool(cond, symbolic=False)
            if ev is True:
                res.extend(if_true)
            elif ev is False:
                res.extend(if_false)
            else:
                res.append(('if', cond, if_true, if_false))

        else:
            res.append(line)

    return res

def sizeof(exp): # returns size of expression in *bits*
    if exp ~ ('storage', :size, ...):
        return size

    if exp ~ ('mask_shl', :size, ...):
        return size

    if exp ~ (:op, _, :size_bytes) and is_array(op):
        return bits(size_bytes)

    if exp ~ ('mem', ('range', _, :size_bytes)):
        return bits(size_bytes)

    if exp ~ ('mem', :idx):
        assert False

    return None

assert sizeof(('mask_shl', 96, 160, 0, 'x')) == 96
assert sizeof(('mem', ('range', 64, 32))) == 32*8
assert sizeof('x') == None

@cached
def find_mems(exp):
    def f(exp):
        if exp ~ ('mem', ...):
            return set([exp])
        else:
            return set()

    return find_f_set(exp, f)

test_e = ('x', 'sth',('mem',4),('t', ('mem', 4), ('mem',8),('mem',('mem',64))))
assert find_mems(test_e) == {('mem', 64), ('mem', ('mem', 64)), ('mem', 4), ('mem', 8)}, find_mems(test_e)


def _eval_msize(cond):
    if opcode(cond) not in ('lt', 'le', 'gt', 'ge'):
        return None

    left, right = cond[1], cond[2]

    if opcode(left) != 'max' and opcode(right) != 'max':
        return None

    if opcode(left) == 'max' and opcode(right) == 'max':
        return None

    if opcode(right) == 'max':
        cond = swap_cond(cond)
        left, right = cond[1], cond[2]

    assert opcode(left) == 'max'

    if opcode(cond) in ('lt', 'le'):

        if opcode(cond) == 'le':
            cond = ('lt', left, add_op(1, right))
            left, right = cond[1], cond[2]

        # max(2,3) <= 3
        # max(2,3) < 4

        # cond == (lt, max(....), right)
        # any .... > right -> True
        # any .... ? right -> ?
        # else -> all ... < right -> False

        if all([safe_lt_op(l, right) is True for l in left[1:]]):
            return False

        if any([safe_lt_op(right, l) is False for l in left[1:]]):
            return False

        if all([safe_le_op(right, l) is True for l in left[1:]]):
            return True

    if opcode(cond) in ('gt', 'ge'):
        assert False, cond  # unsupported yet

    return None


def cleanup_msize(trace, current_msize=0):
    res = []

    for line in trace:
        if opcode(line) == 'setmem':
            line = replace(line, 'msize', current_msize)

            mem_right = memloc_right(line)

            current_msize = _max_op(current_msize, mem_right)

            res.append(line)

        elif opcode(line) == 'while':
            new_one = while_max_memidx(line)
            current_msize = _max_op(current_msize, new_one)
            res.append(line)

        elif opcode(line) == 'if':
            cond, if_true, if_false = line[1:]
            if 'msize' in str(cond) and opcode(current_msize) == 'max':
                tmp_cond = replace(cond, 'msize', current_msize)

                tmp_evald = _eval_msize(tmp_cond)

                if tmp_evald is not None:
                    cond = 1 if tmp_evald is True else 0

            else:
                new_msize = max_to_add(current_msize)
                cond = replace(cond, 'msize', new_msize)

            if_true = cleanup_msize(if_true, current_msize)
            if_false = cleanup_msize(if_false, current_msize)
            res.append(('if', cond, if_true, if_false))

        else:
            line = replace(line, 'msize', current_msize)
            res.append(line)

#    print('done')
    return res


def overwrites_mem(line, mem_idx):
    '''
        for a given line, returns True if it potentially
        overwrites *any part* of memory index, False if it *for sure* doesn't

    '''
    if line ~ ('setmem', :set_idx, _):
        if range_overlaps(set_idx, mem_idx) is not False:
            return True
        else:
            return False

    if line ~ ('while', ...):
        return while_touches_mem(line, mem_idx)

    return False


def affects(line, exp):
    if type(exp) != tuple and exp != 'msize':
        return False

    s = str(exp)

    if 'msize' in s:
        if overwrites_mem(line, ('range', 0, 'undefined')):
            return True

    if 'mem' not in s:
        return False

    mems = find_mems(exp)

    for m in mems:
        m_idx = m[1]
        if overwrites_mem(line, m_idx):
            return True

    return False

line_test = ('setmem', ('range', 65, 32), 'x')
exp_test = ('mul', 8, ('mem', ('range', 64, 32)))
assert affects(line_test, exp_test) == True
exp_test = ('mul', 8, ('mem', ('range', 100, 32)))
assert affects(line_test, exp_test) == False

line_test = ('setmem', ('range', 65, 32), 'x')
exp_test = ('mul', 8, ('mem', ('range', 64, 32)))
assert affects(line_test, exp_test) == True
exp_test = ('mul', 8, ('mem', ('range', 100, 32)))
assert affects(line_test, exp_test) == False

line_test = ('setmem', ('range', 65, 'sth'), 'x')
exp_test = ('mul', 8, ('mem', ('range', 64, 32)))
assert affects(line_test, exp_test) == True
exp_test = ('mul', 8, ('mem', ('range', 100, 32)))
assert affects(line_test, exp_test) == True

line_test = ('setmem', ('range', 65, 32), 'x')
exp_test = ('mul', 8, ('mem', ('range', 64, 1)))
assert affects(line_test, exp_test) == False
exp_test = ('mul', 8, ('mem', ('range', 64, 'sth')))
assert affects(line_test, exp_test) == True


'''
    
    Memory cleanup

'''

def trace_uses_mem(trace, mem_idx):
    '''

        checks if memory is used anywhere in the trace

    '''

    for idx, line in enumerate(trace):

        if line ~ ('setmem', :memloc, :memval):
            memval = simplify_exp(memval)

            if exp_uses_mem(memval, mem_idx):
                return True

            split = memloc_overwrite(mem_idx, memloc) # returns range that we're confident wasn't overwritten by memloc
            res2 = trace[idx+1:]
            for s_idx in split:
                if trace_uses_mem(res2, s_idx):
                    return True

            return False

        elif line ~ ('while', ...):
            if while_uses_mem(line, mem_idx):
                return True

        elif line ~ ('if', :cond, :if_true, :if_false):

            if exp_uses_mem(cond, mem_idx) or \
                trace_uses_mem(if_true, mem_idx) or \
                trace_uses_mem(if_false, mem_idx):
                    return True

        elif line ~ ('continue', ...):
            return True

        else:
            if exp_uses_mem(line, mem_idx):
                return True

    return False


def cleanup_mems(trace, in_loop=False):
    '''
        for every setmem, replace future occurences of it with it's value,
        if possible

    '''

    #pprint_trace(trace)

    res = []

    for idx, line in enumerate(trace):
#        print(line)
        if line ~ ('setmem', :rng, ('mem', rng)):
            continue

        if opcode(line) in ['call', 'staticcall', 'delegatecall', 'codecall']:
            fname, fdata = line[-2:]

            if fdata ~ ('mem', ('range', _, -4)):
                line = line[:-2] + (None, None)

            res.append(line)

        elif line ~ ('setmem', :mem_idx, :mem_val):
            # find all the future occurences of var and replace if possible
            if not affects(line, mem_val): 
                remaining_trace = replace_mem(trace[idx+1:], mem_idx, mem_val)
            else:
                remaining_trace = trace[idx+1:]

            if in_loop or trace_uses_mem(remaining_trace, mem_idx):
                res.append(line)

            res.extend(cleanup_mems(remaining_trace))

            break

        elif line ~ ('while', :cond, :path, *rest):

            path = cleanup_mems(path)
            res.append(('while', cond, path, ) + rest)

        elif line ~ ('if', :cond, :if_true, :if_false):
            if_true = cleanup_mems(if_true)
            if_false = cleanup_mems(if_false)
            res.append(('if', cond, if_true, if_false))

        else:
            res.append(line)

    return res

cache_replace_mem_exp = {}

@cached
def replace_mem_exp(exp, mem_idx, mem_val):
    if type(exp) != tuple:
        return exp

    res = tuple(replace_mem_exp(e, mem_idx, mem_val) if type(e) == tuple else e for e in exp)

    if opcode(mem_val) not in ('mem', 'var', 'data'):
        if res ~ ('delegatecall', :gas, :addr, ('mem', :func), ('mem', :args)):
            assert func ~ ('range', :f_begin, :f_len)
            assert args ~ ('range', :a_begin, :a_len)
            if f_len == 4 and sub_op(add_op(f_begin, f_len), a_begin) == 0: #:
                # we have a situation when inside memory is sth like: (range 96 4) (100 ...)
                # let's merge those two memories, and try to replace with mem exp
                res_range = simplify_exp(('range', f_begin, add_op(f_len, a_len)))
                if res_range == mem_idx:
                    res = ('delegatecall', gas, addr, None, mem_val)

        if res ~ ('call', :gas, :addr, :value, ('mem', :func), ('mem', :args)):
            assert func ~ ('range', :f_begin, :f_len)
            assert args ~ ('range', :a_begin, :a_len)
            if f_len == 4 and sub_op(add_op(f_begin, f_len), a_begin) == 0: #:
                # we have a situation when inside memory is sth like: (range 96 4) (100 ...)
                # let's merge those two memories, and try to replace with mem exp
                res_range = simplify_exp(('range', f_begin, add_op(f_len, a_len)))

                if res_range == mem_idx:

                    res = ('call', gas, addr, value, None, mem_val)


    if res != exp:
        res = simplify_exp(res)

    if res ~ ('mem', ...):
        assert res ~ ('mem', _), res
        res = fill_mem(res, mem_idx, mem_val)

    return res


def replace_mem(trace, mem_idx, mem_val):
    '''

        replaces any reference to mem_idx in the trace
        with a value of mem_val, up until a point of that mem being
        overwritten

        mem[64] = 'X'
        log mem[64]
        mem[65] = 'Y'
        log mem[64 len 1]
        log mem[65]
        mem[63] = 'Z'
        ...

        into

        mem[64] = 'X'
        log 'X'
        mem[65] = 'Y'
        log mask(1, 'X')
        log mem[65]
        ... (the rest unchanged)

    '''
    mem_idx = simplify_exp(mem_idx)
    mem_val = simplify_exp(mem_val)
    mem_id = ('mem', mem_idx)

    if type(mem_val) is tuple and opcode(mem_val) != 'mem':
        mem_val = arithmetic.eval(mem_val)

    res = []

    for idx, line in enumerate(trace):

        if line ~ ('setmem', :memloc, _):
            memloc = simplify_exp(memloc)
            # replace in val
            res.append(replace_mem_exp(line, mem_idx, mem_val))
            if range_overlaps(memloc, mem_idx):
                split = splits_mem(mem_idx, memloc, mem_val)
                res2 = trace[idx+1:]
                for s in split:
                    res2 = replace_mem(res2, s[0], s[1])

                res.extend(res2)
                return res
            if affects(line, mem_val):
                res.extend(copy(trace[idx+1:]))
                return res

        elif affects(line, mem_val) or affects(line, mem_id):
            res.extend(copy(trace[idx:]))
            return res

        elif line ~ ('while', :cond, :path, :jds, :vars):
                # shouldn't this go above the affects if above? and also update vars even if
                # the loops affects the memidx?

            xx = []
            for v in vars:
                xx.append(replace_mem_exp(v, mem_idx, mem_val))
            vars = xx

            if not affects(line, ('mem', mem_idx)) and \
               not affects(line, (mem_val)):
                cond = replace_mem_exp(cond, mem_idx, mem_val)
                path = replace_mem(path, mem_idx, mem_val)

            res.append(('while', cond, path, jds, vars))


        elif line ~ ('if', :cond, :if_true, :if_false):

            cond = replace_mem_exp(cond, mem_idx, mem_val)
            mem_idx_true = apply_constraint(mem_idx, cond)
            mem_val_true = apply_constraint(mem_val, cond)
            mem_idx_false = apply_constraint(mem_idx, is_zero(cond))
            mem_val_false = apply_constraint(mem_val, is_zero(cond))


            if_true = replace_mem(if_true, mem_idx, mem_val)
            if_false = replace_mem(if_false, mem_idx, mem_val)

            res.append(('if', cond, if_true, if_false))


        else:
            # speed
            test = 'mem' in str(line)
            if test and mem_idx ~ ('add', _, ('var', :num)) and \
                str(('var', num)) not in str(line):
                test = False
            # / speed

            if test:
                l = replace_mem_exp(line, mem_idx, mem_val)
            else:
                l = line

            res.append(l)

    return res


'''

    Variables cleanup

'''

def cleanup_vars(trace, required_after = None):
    required_after = required_after or []
    '''
        for every var = mem declaration, replace future
        occurences of it, if possible

        var1 = mem[64]
        log var1
        mem[65] = 'Y'
        log var1

        into

        var1 = mem[64]
        log mem[64]
        mem[65] = 'Y'
        log var1

        for var declarations that are no longer in use, remove them

    '''

    res = []

    for idx, line in enumerate(trace):
        if line ~ ('setvar', :var_idx, :var_val):
            # find all the future occurences of var and replace if possible

            remaining_trace = replace_var(trace[idx+1:], var_idx, var_val)
            if contains(remaining_trace, ('var', var_idx)) or \
                ('var', var_idx) in required_after:
                    res.append(line)

            res.extend(cleanup_vars(remaining_trace, required_after=required_after))
            return res


        elif line ~ ('while', :cond, :path, *rest):
            path = cleanup_vars(path, required_after=required_after + find_op_list(trace[idx+1:], 'var'))
            res.append(('while', cond, path, ) + rest)

            a = parse_counters(line)

            if 'endvars' in a:
                remaining_trace = trace[idx+1:]
                for var_idx, var_val in a['endvars'].items():
                    remaining_trace = replace_var(remaining_trace, var_idx, var_val)

                res.extend(cleanup_vars(remaining_trace, required_after=required_after + find_op_list(trace[idx+1:], 'var')))
                return res

        elif line ~ ('if', :cond, :if_true, :if_false):
            if_true = cleanup_vars(if_true, required_after=required_after)
            if_false = cleanup_vars(if_false, required_after=required_after)
            res.append(('if', cond, if_true, if_false))
        else:
            res.append(line)

    return res

def replace_var(trace, var_idx, var_val):
    '''
        replace occurences of var, if possible

    '''

    var_id = ('var', var_idx)
    res = []

    for idx, line in enumerate(trace):

        if line ~ ('setmem', :mem_idx, _):
            # this all seems incorrect, (plus 'affects' checks below, needs to be revisited)
            memloc = ('mem', mem_idx)
            # replace in val
            res.append(replace(line, var_id, var_val))

            if affects(line, var_val):
                res.extend(copy(trace[idx+1:]))
                return res
            else:
                continue

        if line ~ ('while', :cond, :path, :jd, :setvars):
            setvars = replace(setvars, var_id, var_val)

            if not affects(line, var_val): #and not find_f(path, lambda e: e ~ ('setvar', var_idx, _)):
                cond = replace(cond, var_id, var_val)
                path = replace_var(path, var_idx, var_val)


            line = ('while', cond, path, jd, setvars)

        if affects(line, var_val):
            res.append(line)
            res.extend(copy(trace[idx+1:]))
            return res

        elif opcode(line) == 'while':
            assert not affects(line, var_val)
            res.append(line) # could replace vars inside of while, skipping for now

        elif line ~ ('if', :cond, :if_true, :if_false):
            cond = replace(cond, var_id, var_val)
            if_true = replace_var(if_true, var_idx, var_val)
            if_false = replace_var(if_false, var_idx, var_val)
            res.append(('if', cond, if_true, if_false))

        else:
            res.append(replace(line, var_id, var_val))

    return res


'''

    loop parsing

'''


def find_conts(trace):
    def check(line):
        if opcode(line) == 'continue':
            return [line]
        else:
            return []

    return find_f_list(trace, check)


def swap_cond(cond):
    replacement = {
        'lt': 'gt',
        'le': 'ge',
        'gt': 'lt',
        'ge': 'le',
    }

    return (replacement[cond[0]], cond[2], cond[1])



def move_right(left, right, exp):
    assert type(right) != list
    assert type(left) != list
    if left == exp:
        return right

    if left ~ ('add', *terms):
        assert exp in terms, terms # deep embedding unsupported
        for e in terms:
            if e != exp:
                if type(e) == int:
                    e = to_real_int(e)
                right = sub_op(right, e)

        return right

    if left ~ ('mul', *terms):
        assert exp in terms # deep embedding unsupported
        for e in terms:
            if e != exp:
                assert type(e) != list
                assert type(right) != list
                right = div_op(right, e)

        return right

def normalize(cond):
    cond = tuple(cleanup_mul_1(cond))

    if opcode(cond) not in ('lt', 'le', 'gt', 'ge'):
        cond = ('lt', 0, cond)
        return normalize(cond)

    left, right = cond[1], cond[2]
    vars_left = find_op_list(left, 'var')
    vars_right = find_op_list(right, 'var')

    left_vars = tuple([e for e in vars_left if e ~ ('var', int)]) # int = loop vars
    right_vars = tuple([e for e in vars_right if e ~ ('var', int)]) 

    if len(left_vars) + len(right_vars) != 1:
        return None

    if len(right_vars) == 1:
        return normalize(swap_cond(cond))

    assert len(left_vars) == 1 and len(right_vars) == 0, cond

    var = left_vars[0]

    if opcode(left) != 'var':
        assert type(right) != list
        assert type(left) != list
        right = move_right(left, right, var)
        left = var
        cond = (cond[0], left, right)

    if cond ~ ('lt', :left, :right):
        cond = ('le', left, sub_op(right, 1))

    if cond ~ ('gt', :left, :right):
        cond = ('ge', left, add_op(right, 1))

    return cond # we end up with (gt/lt (var int) sth)


def find_setmems(trace):
    def check(line):
        if line ~ ('while', _, :path, ...):

            sm = find_setmems(path)
            if len(sm) == 0:
                return []

            for s in sm:
                s_idx = s[1]
#                if 'var' in str(s_idx):
#                    print(s_idx)
#                    assert False

            return sm

        elif line ~ ('setmem', ...):
            return [line]

        else:
            return []

    return walk_trace(trace, check)

def memloc_left(setmem):
    assert opcode(setmem) in ('setmem', 'mem')
    memloc = setmem[1]
    assert memloc ~ ('range', :loc, _)
    return loc

def memloc_right(setmem):
    assert opcode(setmem) in ('setmem', 'mem')
    memloc = setmem[1]

    assert memloc ~ ('range', :loc, :rlen)
    return add_op(loc, rlen)

def make_range(left, right):
    r_len = sub_op(right, left)

    if safe_ge_zero(r_len) is False:
        return ('range', left, 0)
    else:
        return ('range', left, r_len)

def while_max_memidx(line):
    # returns the rightmost memory index for a setmem

    a = parse_counters(line)
    assert line ~ ('while', :cond, :path, :jds, :setvars)

    try:
        setmems = find_setmems(path)
    except:
        return 'unknown'

    if len(setmems) == 0:
        return 0

    collected = 0

    if 'endvars' not in a:
        for s in setmems:
            collected = _max_op(collected, memloc_right(s))

        return collected

    setmems_begin = setmems_end = setmems


    for v in a['setvars']:
        v_idx, v_start = v[1], v[2]
        v_end = a['endvars'][v_idx]

        setmems_begin = replace_var(setmems_begin, v_idx, v_start)
        setmems_end = replace_var(setmems_end, v_idx, v_end)

    for idx, _ in enumerate(setmems):
        collected = _max_op(collected, memloc_right(setmems_begin[idx]))
        collected = _max_op(collected, memloc_right(setmems_end[idx]))


    return collected

def extract_paths(while_exp):
    assert while_exp ~ ('while', _, :trace, :jd, :setvars)

    paths = []
    def f(trace, jd, so_far):
        # extract all the paths leading up to jd
        if len(trace) == 0:
            return []

        line = trace[0]

        #assert opcode(line) != 'while'


        if line ~ ('if', :cond, :if_true, :if_false):
            res_true = f(if_true, jd, so_far + [('require', cond)])
            res_false = f(if_false, jd, so_far + [('require', is_zero(cond))])
            return res_true + res_false


        if len(trace) == 1:
            if line ~ ('continue', ...):
                return [so_far]
            else:
                return []


        return f(trace[1:], jd, so_far + [line])

    return f(trace, jd, [])

def extract_setmems(while_exp):
    paths = extract_paths(while_exp)
    res = []
    for p in paths:
        res += find_setmems(p)
    return res

def extract_mems(while_exp):
    paths = extract_paths(while_exp)
    res = []
    for p in paths:
        res += find_mems(p)
    return res

#        mems = extract_mems(path)


def while_touches_mem(line, mem_idx):
    a = parse_counters(line)
    assert line ~ ('while', :cond, :path, :jds, :setvars)
    cond, path, jds, setvars = line[1:]

#    try:
    setmems = extract_setmems(line)
#    setmems = find_setmems(path)
#    except:
#        return True

    if len(setmems) == 0:
        return False

    setmems_begin = setmems_end = setmems

    if 'endvars' not in a:
        for s in setmems: # if no endvars, comparing just with a 'var' assumes 'var' is any natural number
            if range_overlaps(mem_idx, s[1]) is not False:
                return True

        return False

    for v in a['setvars']:
        v_idx, v_start = v[1], v[2]
        v_end = a['endvars'][v_idx]

        setmems_begin = replace_var(setmems_begin, v_idx, v_start)
        setmems_end = replace_var(setmems_end, v_idx, v_end)

    for idx, _ in enumerate(setmems):
        r_begin = memloc_left(setmems_begin[idx])
        r_end = memloc_right(setmems_end[idx])

        r = make_range(r_begin, r_end)
        if range_overlaps(mem_idx, r) is not False:
            return True

        r_begin = memloc_left(setmems_end[idx])
        r_end = memloc_right(setmems_begin[idx])

        r = make_range(r_begin, r_end)
        if range_overlaps(mem_idx, r) is not False:
            return True

    return False

def while_uses_mem(line, mem_idx):
    assert line ~ ('while', :cond, :path, :jds, :setvars)
    a = parse_counters(line)

    mems = find_mems(line)

#    mems = extract_mems(line)

    if len(mems) == 0:
        return False

    mems_begin = mems_end = mems

    if 'endvars' not in a:

        for s in mems:
            if range_overlaps(mem_idx, s[1]) is not False:
                return True

        return False

    for v in a['setvars']:
        v_idx, v_start = v[1], v[2]
        v_end = a['endvars'][v_idx]

        mems_begin = replace_var(mems_begin, v_idx, v_start)
        mems_end = replace_var(mems_end, v_idx, v_end)

    for idx, _ in enumerate(mems):
        r_begin = memloc_left(mems_begin[idx])
        r_end = memloc_right(mems_end[idx])

        r = make_range(r_begin, r_end)
        if range_overlaps(mem_idx, r) is not False:
            return True

        r_begin = memloc_left(mems_end[idx])
        r_end = memloc_right(mems_begin[idx])

        r = make_range(r_begin, r_end)
        if range_overlaps(mem_idx, r) is not False:
            return True

    return False

def exp_uses_mem(exp, mem_idx):
    mems = find_mems([exp])

    for m in mems:
        assert m ~ ('mem', :m_idx)
        if range_overlaps(m_idx, mem_idx) is not False:
            return True

    return False


def parse_counters(line):

    a = {}
    assert line ~ ('while', :cond, :path, :jds, :setvars)

    a['setvars'] = setvars
    a['jds'] = jds

    conts = find_conts(path)
#    print(conts)
#    print(find_op_list(path, 'continue'))
    assert conts == find_op_list(path, 'continue')

    startvars = {}
    for v in setvars:
        assert v ~ ('setvar', :vidx, :vval)
        startvars[vidx] = vval

    cond = normalize(cond)
    if cond is None:
        return {}

    cont = conts[0]

    stepvars = {}
    for v in cont[2]:
        var_idx, var_val = v[1], v[2]
        stepvars[var_idx] = var_val

    a['stepvars'] = stepvars

    counter = cond[1][1] 
    counter_stop = cond[2]
    counter_start = startvars[counter]
    a['counter'] = counter
    a['start'] = counter_start
    a['stop'] = counter_stop
    if counter not in stepvars:
        logger.warn(f'counter not in stepvars')
        counter_diff = 0
    else:
        counter_diff = stepvars[counter]

    if len(conts) > 1:
        return a

    if opcode(counter_diff) != 'add':
        return a

    assert type(counter_diff[1]) == int

    counter_diff = (counter_diff[0], to_real_int(counter_diff[1]), counter_diff[2])

    # counter_diff[2] ~ ('mul', 1, X) -> counter_diff[2] = X
    if opcode(counter_diff[2]) == 'mul' and counter_diff[2][1] == 1:
        counter_diff = (counter_diff[0], counter_diff[1], counter_diff[2][2])

    assert counter_diff[2] == ('var', counter), counter_diff

    counter_step = to_real_int(counter_diff[1])
    a['step'] = counter_step

    num_loops = div_op(add_op(sub_op(counter_stop, counter_start), counter_step), counter_step)

    if num_loops ~ ('div', ...): # so, no obvious divider
        a['counter_stop'] = counter_stop
        a['counter_start'] = counter_start
        a['counter_step'] = counter_step
        return a

    a['num_loops'] = num_loops

    a['endvars'] = {}
    for v in setvars:
        var_idx, var_val = v[1], to_real_int(v[2])
        var_diff = to_real_int(stepvars[var_idx][1])
        assert type(num_loops) != list
        var_stop = add_op(var_val, mul_op(var_diff, num_loops))
        a['endvars'][var_idx] = var_stop

    return a

'''

    make whiles

'''


def make(trace):
    res = []

    for idx, line in enumerate(trace):
        if line ~ ('if', :cond, :if_true, :if_false):
            res.append(('if', cond, make(if_true), make(if_false)))

        elif line ~ ('label', :jd, :vars, ...):
            try:
                before, inside, remaining, cond = to_while(trace[idx+1:], jd)
            except:
                continue
                return trace

            inside = inside #+ [str(inside)]

            inside = make(inside)
            remaining = make(remaining)

            for _, v_idx, v_val in vars:
                before = replace(before, ('var', v_idx), v_val)
            before = make(before)


            res.extend(before)
            res.append(('while', cond, inside, repr(jd), vars))
            res.extend(remaining)

            return res

        elif line ~ ('goto', :jd, :setvars):
            res.append(('continue', repr(jd), setvars))

        else:
            res.append(line)

    return res


def get_jds(line):
    if line ~ ('goto', :jd, ...):
        return [jd]

    else:
        return []

def is_revert(trace):
    if len(trace) > 1:
        return False

    line = trace[0]
    if (line ~ ('return', 0)) or \
       (line ~ ('revert', ...)) or \
       (line ~ ('invalid', ...)):
        return True

    return False

def to_while(trace, jd, path = None):
    path = path or []

    while True:
        if trace == []:
            raise
        line = trace[0]
        trace = trace[1:]

        if (line ~ ('if', :cond, :if_true, :if_false)):
            if is_revert(if_true):
                path.append(('require', is_zero(cond)))
                trace = if_false
                continue

            if is_revert(if_false):
                path.append(('require', cond))
                trace = if_true
                continue

            jds_true = find_f_list(if_true, get_jds)
            jds_false = find_f_list(if_false, get_jds)

            assert (jd in jds_true) != (jd in jds_false), (jds_true, jds_false)

            def add_path(line):
                if line ~ ('goto', _, :svs):
                    path2 = path
                    for _, v_idx, v_val in svs:
                        path2 = replace(path2, ('var', v_idx), v_val)

                    return path2 + [line]
                else:
                    return [line]

            if jd in jds_true:
                if_true = rewrite_trace(if_true, add_path)
                return path, if_true, if_false, cond
            else:
                if_false = rewrite_trace(if_false, add_path)
                return path, if_false, if_true, is_zero(cond)

        else:
            path.append(line)


    assert False, f'no if after label?{jd}'
