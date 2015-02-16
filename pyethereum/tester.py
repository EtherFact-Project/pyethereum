import shutil
import tempfile
import time
import logging
import sys
import spv
import pyethereum
import pyethereum.db as db
import pyethereum.opcodes as opcodes
import pyethereum.abi as abi
from pyethereum.slogging import get_logger, LogRecorder, configure_logging

serpent = None

u = pyethereum.utils
t = pyethereum.transactions
b = pyethereum.blocks
pb = pyethereum.processblock
vm = pyethereum.vm

accounts = []
keys = []

for i in range(10):
    keys.append(u.sha3(str(i)))
    accounts.append(u.privtoaddr(keys[-1]))

k0, k1, k2, k3, k4, k5, k6, k7, k8, k9 = keys[:10]
a0, a1, a2, a3, a4, a5, a6, a7, a8, a9 = accounts[:10]

languages = {}

seed = 3 ** 160


def dict_without(d, *args):
    o = {}
    for k, v in d.items():
        if k not in args:
            o[k] = v
    return o


def dict_with(d, **kwargs):
    o = {}
    for k, v in d.items():
        o[k] = v
    for k, v in kwargs.items():
        o[k] = v
    return o


# Pseudo-RNG (deterministic for now for testing purposes)
def rand():
    global seed
    seed = pow(seed, 2, 2 ** 512)
    return seed % 2 ** 256


class state():

    def __init__(self, num_accounts=len(keys)):
        global serpent
        if not serpent:
            serpent = __import__('serpent')

        self.temp_data_dir = tempfile.mkdtemp()
        self.db = db.DB(u.db_path(self.temp_data_dir))

        o = {}
        for i in range(num_accounts):
            o[accounts[i]] = 10 ** 24
        self.block = b.genesis(self.db, o)
        self.blocks = [self.block]
        self.block.timestamp = 1410973349
        self.block.coinbase = a0
        self.block.gas_limit = 10 ** 9

    def __del__(self):
        shutil.rmtree(self.temp_data_dir)

    def contract(self, code, sender=k0, endowment=0):
        evm = serpent.compile(code)
        o = self.evm(evm, sender, endowment)
        assert len(self.block.get_code(o)), "Contract code empty"
        return o

    def abi_contract(me, code, sender=k0, endowment=0, language='serpent'):

        class _abi_contract():

            def __init__(self, _state, code, sender=k0,
                         endowment=0, language='serpent'):
                if language not in languages:
                    languages[language] = __import__(language)
                language = languages[language]
                evm = language.compile(code)
                self.address = me.evm(evm, sender, endowment)
                assert len(me.block.get_code(self.address)), \
                    "Contract code empty"
                sig = language.mk_full_signature(code)
                self._translator = abi.ContractTranslator(sig)

                def kall_factory(f):

                    def kall(*args, **kwargs):
                        o = _state._send(kwargs.get('sender', k0),
                                         self.address,
                                         kwargs.get('value', 0),
                                         self._translator.encode(f, args),
                                         **dict_without(kwargs, 'sender',
                                                        'value', 'output'))
                        # Compute output data
                        if kwargs.get('output', '') == 'raw':
                            outdata = o['output']
                        elif not o['output']:
                            outdata = None
                        else:
                            outdata = self._translator.decode(f, o['output'])
                            outdata = outdata[0] if len(outdata) == 1 \
                                else outdata
                        # Format output
                        if kwargs.get('profiling', ''):
                            return dict_with(o, output=outdata)
                        else:
                            return outdata
                    return kall

                for f in self._translator.function_data:
                    vars(self)[f] = kall_factory(f)

        return _abi_contract(me, code, sender, endowment, language)

    def evm(self, evm, sender=k0, endowment=0):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        tx = t.contract(sendnonce, 1, gas_limit, endowment, evm)
        tx.sign(sender)
        (s, a) = pb.apply_transaction(self.block, tx)
        if not s:
            raise Exception("Contract creation failed")
        return a

    def call(*args, **kwargs):
        raise Exception("Call deprecated. Please use the abi_contract "
                        "mechanism or send(sender, to, value, "
                        "data) directly, using the abi module to generate "
                        "data if needed")

    def _send(self, sender, to, value, evmdata='', output=None,
              funid=None, abi=None, profiling=0):
        if funid is not None or abi is not None:
            raise Exception("Send with funid+abi is deprecated. Please use"
                            " the abi_contract mechanism")
        tm, g = time.time(), self.block.gas_used
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        recorder = LogRecorder() if profiling > 1 else None
        (s, o) = pb.apply_transaction(self.block, tx)
        if not s:
            raise Exception("Transaction failed")
        out = {"output": o}
        if profiling > 0:
            zero_bytes = tx.data.count(chr(0))
            non_zero_bytes = len(tx.data) - zero_bytes
            intrinsic_gas_used = opcodes.GTXDATAZERO * zero_bytes + \
                opcodes.GTXDATANONZERO * non_zero_bytes
            ntm, ng = time.time(), self.block.gas_used
            out["time"] = ntm - tm
            out["gas"] = ng - g - intrinsic_gas_used
        if profiling > 1:
            trace = recorder.pop_records()
            ops = [x['op'] for x in trace if x['event'] == 'vm']
            opdict = {}
            for op in ops:
                opdict[op] = opdict.get(op, 0) + 1
            out["ops"] = opdict
        return out

    def profile(self, *args, **kwargs):
        kwargs['profiling'] = True
        return self._send(*args, **kwargs)

    def send(self, *args, **kwargs):
        return self._send(*args, **kwargs)["output"]

    def mkspv(self, sender, to, value, data=[], funid=None, abi=None):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        if funid is not None:
            evmdata = serpent.encode_abi(funid, *abi)
        else:
            evmdata = serpent.encode_datalist(*data)
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        return spv.mk_transaction_spv_proof(self.block, tx)

    def verifyspv(self, sender, to, value, data=[],
                  funid=None, abi=None, proof=[]):
        sendnonce = self.block.get_nonce(u.privtoaddr(sender))
        if funid is not None:
            evmdata = serpent.encode_abi(funid, *abi)
        else:
            evmdata = serpent.encode_datalist(*data)
        tx = t.Transaction(sendnonce, 1, gas_limit, to, value, evmdata)
        self.last_tx = tx
        tx.sign(sender)
        return spv.verify_transaction_spv_proof(self.block, tx, proof)

    def trace(self, sender, to, value, data=[]):
        # collect log events (independent of loglevel filters)
        recorder = LogRecorder()
        self.send(sender, to, value, data)
        return recorder.pop_records()

    def mine(self, n=1, coinbase=a0):
        for i in range(n):
            self.block.finalize()
            t = self.block.timestamp + 6 + rand() % 12
            self.block = b.Block.init_from_parent(self.block, coinbase, '', t)
            self.blocks.append(self.block)

    def snapshot(self):
        return self.block.serialize()

    def revert(self, data):
        self.block = b.Block.deserialize(self.db, data)

# logging


def set_logging_level(lvl=1):
    trace_lvl_map = [
        ':info',
        'eth.vm.log:trace',
        ':info,eth.vm.log:trace,eth.vm.exit:trace',
        ':info,eth.vm.log:trace,eth.vm.op:trace,eth.vm.stack:trace',
        ':info,eth.vm.log:trace,eth.vm.op:trace,eth.vm.stack:trace,' +
        'eth.vm.storage:trace,eth.vm.memory:trace'
    ]
    configure_logging(config_string=trace_lvl_map[lvl])
    print 'Set logging level: %d' % lvl


def set_log_trace(logger_names=[]):
    """
    sets all named loggers to level 'trace'
    attention: vm.op.* are only active if vm.op is active
    """
    for name in logger_names:
        assert name in slogging.get_logger_names()
        slogging.set_level(name, 'trace')


def enable_logging():
    set_logging_level(1)


def disable_logging():
    set_logging_level(0)


gas_limit = 1000000
