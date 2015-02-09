import sys
import types

import ana
import pyvex
import simuvex
import logging
l = logging.getLogger("angr.vexer")

class SerializableIRSB(ana.Storable):
    __slots__ = [ '_state', '_irsb', '_addr' ]

    def __init__(self, *args, **kwargs):
        self._state = args, kwargs
        self._irsb = pyvex.IRSB(*args, **kwargs)
        self._addr = next(a.addr for a in self._irsb.statements() if type(a) is pyvex.IRStmt.IMark)

    def __dir__(self):
        return dir(self._irsb) + self._all_slots()

    def __getattr__(self, a):
        try:
            return object.__getattribute__(self, a)
        except AttributeError:
            return getattr(self._irsb, a)

    def __setattr__(self, a, v):
        try:
            return object.__setattr__(self, a, v)
        except AttributeError:
            return setattr(getattr(self._irsb, a, v))

    def _ana_getstate(self):
        return self._state

    def _ana_setstate(self, s):
        self.__init__(*(s[0]), **(s[1]))

    def _ana_getliteral(self):
        return self._crawl_vex(self._irsb)

    def instruction_addrs(self):
        return [ s.addr for s in self._irsb.statements() if type(s) is pyvex.IRStmt.IMark ]

    @property
    def json(self):
        return self._ana_getliteral()

    def _crawl_vex(self, p):
        if type(p) in (int, str, float, long, bool): return p
        elif type(p) in (tuple, list, set): return [ self._crawl_vex(e) for e in p ]
        elif type(p) is dict: return { k:self._crawl_vex(p[k]) for k in p }

        attr_keys = set()
        for k in dir(p):
            if k in [ 'wrapped' ] or k.startswith('_'):
                continue

            if type(getattr(p, k)) in (types.BuiltinFunctionType, types.BuiltinMethodType, types.FunctionType, types.ClassType, type):
                continue

            attr_keys.add(k)

        vdict = { }
        for k in attr_keys:
            vdict[k] = self._crawl_vex(getattr(p, k))

        if type(p) is pyvex.IRSB:
            vdict['statements'] = self._crawl_vex(p.statements())
            vdict['instructions'] = self._crawl_vex(p.instructions())
            vdict['addr'] = self._addr
        elif type(p) is pyvex.IRTypeEnv:
            vdict['types'] = self._crawl_vex(p.types())

        return vdict

class VEXer:
    def __init__(self, mem, arch, max_size=None, num_inst=None, traceflags=None, use_cache=None):
        self.mem = mem
        self.arch = arch
        self.max_size = 400 if max_size is None else max_size
        self.num_inst = 99 if num_inst is None else num_inst
        self.traceflags = 0 if traceflags is None else traceflags
        self.use_cache = True if use_cache is None else use_cache
        self.irsb_cache = { }


    def block(self, addr, max_size=None, num_inst=None, traceflags=0, thumb=False, backup_state=None):
        """
        Returns a pyvex block starting at address addr

        Optional params:

        @param max_size: the maximum size of the block, in bytes
        @param num_inst: the maximum number of instructions
        @param traceflags: traceflags to be passed to VEX. Default: 0
        """
        max_size = self.max_size if max_size is None else max_size
        num_inst = self.num_inst if num_inst is None else num_inst

        # TODO: FIXME: figure out what to do if we're about to exhaust the memory
        # (we can probably figure out how many instructions we have left by talking to IDA)

        # TODO: remove this ugly horrid hack

        if thumb:
            addr &= ~1

        # Try to find the actual size of the block, stop at the first keyerror
        arr = []
        for i in range(addr, addr+max_size):
            try:
                arr.append(self.mem[i])
            except KeyError:
                if backup_state:
                    if i in backup_state.memory:
                        val = backup_state.mem_expr(backup_state.BVV(i), backup_state.BVV(1))
                        try:
                            val = backup_state.se.exactly_n_int(val, 1)[0]
                            val = chr(val)
                        except simuvex.SimValueError as ex:
                            break

                        arr.append(val)
                    else:
                        break
                else:
                    break

        buff = "".join(arr)

        # deal with thumb mode in ARM, sending an odd address and an offset
        # into the string
        byte_offset = 0

        if thumb:
            byte_offset = (addr | 1) - addr
            addr |= 1

        if not buff:
            raise AngrMemoryError("No bytes in memory for block starting at 0x%x." % addr)

        l.debug("Creating pyvex.IRSB of arch %s at 0x%x", self.arch.name, addr)

        if self.use_cache:
            cache_key = (buff, addr, num_inst, self.arch.vex_arch, byte_offset, thumb)
            if cache_key in self.irsb_cache:
                return self.irsb_cache[cache_key]

        try:
            if num_inst:
                block = SerializableIRSB(bytes=buff, mem_addr=addr, num_inst=num_inst, arch=self.arch.vex_arch,
                                   endness=self.arch.vex_endness, bytes_offset=byte_offset, traceflags=traceflags)
            else:
                block = SerializableIRSB(bytes=buff, mem_addr=addr, arch=self.arch.vex_arch,
                                   endness=self.arch.vex_endness, bytes_offset=byte_offset, traceflags=traceflags)
        except pyvex.PyVEXError:
            l.debug("VEX translation error at 0x%x", addr)
            l.debug("Using bytes: " + buff.encode('hex'))
            e_type, value, traceback = sys.exc_info()
            raise AngrTranslationError, ("Translation error", e_type, value), traceback

        if self.use_cache:
            self.irsb_cache[cache_key] = block

        return block

    def __getitem__(self, addr):
        return self.block(addr)

from .errors import AngrMemoryError, AngrTranslationError
