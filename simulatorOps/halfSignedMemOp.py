import operator
import struct
from enum import Enum
from collections import defaultdict, namedtuple, deque 

import simulatorOps.utils as utils
from simulatorOps.abstractOp import AbstractOp, ExecutionException

class HalfSignedMemOp(AbstractOp):
    saveStateKeys = frozenset(("condition", 
                                "imm", "pre", "sign", "byte", "writeback", "mode", "signed",
                                "basereg", "rd", "offsetImm", "offsetReg"))

    def __init__(self):
        super().__init__()
        self._type = utils.InstrType.memop

    def decode(self):
        instrInt = self.instrInt
        if not (utils.checkMask(instrInt, (7, 4), (27, 26, 25))):
            raise ExecutionException("Le bytecode à cette adresse ne correspond à aucune instruction valide (3)", 
                                        internalError=False)

        # Retrieve the condition field
        self._decodeCondition()

        # This is the inverse of LDR/STR, if bit 22 is set, then offset IS an immediate value
        self.imm = bool(instrInt & (1 << 22))   
        self.pre = bool(instrInt & (1 << 24))
        self.sign = 1 if instrInt & (1 << 23) else -1
        self.byte = not bool(instrInt & (1 << 5))
        self.signed = bool(instrInt & (1 << 6))
        # See 4.9.1 (with post, writeback is redundant and always implicitely on)
        self.writeback = bool(instrInt & (1 << 21)) or not self.pre
        self.mode = "LDR" if instrInt & (1 << 20) else "STR"

        self.basereg = (instrInt >> 16) & 0xF
        self.rd = (instrInt >> 12) & 0xF

        if self.imm:
            # The immediate offset is divided in 2 nibbles:
            # the 4 LSB are at positions [3, 2, 1, 0]
            # the 4 MSB are at positions [11, 10, 9, 8]
            self.offsetImm = instrInt & 0xF + ((instrInt >> 4) & 0xF0)
        else:
            # No shift allowed with these instructions
            self.offsetReg = instrInt & 0xF


    def explain(self, simulatorContext):
        self.resetAccessStates()
        bank = simulatorContext.regs.mode
        simulatorContext.regs.deactivateBreakpoints()
        
        disassembly = self.mode
        description = "<ol>\n"
        disCond, descCond = self._explainCondition()
        description += descCond
        disassembly += disCond

        self._readregs = utils.registerWithCurrentBank(self.basereg, bank)
        addr = baseval = simulatorContext.regs[self.basereg]

        description += "<li>Utilise la valeur du registre {} comme adresse de base</li>\n".format(utils.regSuffixWithBank(self.basereg, bank))
        descoffset = ""
        if self.imm:
            addr += self.sign * self.offsetImm
            if self.offsetImm > 0:
                if self.sign > 0:
                    descoffset = "<li>Additionne la constante #{} à l'adresse de base</li>\n".format(self.offsetImm)
                else:
                    descoffset = "<li>Soustrait la constante #{} à l'adresse de base</li>\n".format(self.offsetImm)
        else:
            regDesc = utils.regSuffixWithBank(self.offsetReg, bank)
            if self.sign > 0:
                descoffset = "<li>Additionne le registre {} à l'adresse de base</li>\n".format(regDesc)
            else:
                descoffset = "<li>Soustrait le registre {} à l'adresse de base</li>\n".format(regDesc)

            addr += self.sign * simulatorContext.regs[self.offsetReg]
            self._readregs |= utils.registerWithCurrentBank(self.offsetReg, bank)

        realAddr = addr if self.pre else baseval
        sizeaccess = 1 if self.byte else 2
        sizedesc = "1 octet" if sizeaccess == 1 else "{} octets".format(sizeaccess)

        disassembly += "S" if self.signed else ""
        disassembly += "B" if sizeaccess == 1 else "H" if sizeaccess == 2 else ""
        disassembly += " R{}, [R{}".format(self.rd, self.basereg)

        if self.mode == 'LDR':
            if self.pre:
                description += descoffset
                description += "<li>Lit {} à partir de l'adresse obtenue (pré-incrément) et stocke le résultat dans {} (LDR)</li>\n".format(sizedesc, utils.regSuffixWithBank(self.rd, bank))
            else:
                description += "<li>Lit {} à partir de l'adresse de base et stocke le résultat dans {} (LDR)</li>\n".format(sizedesc, utils.regSuffixWithBank(self.rd, bank))
                description += descoffset
            
            if self.signed:
                description += "<li>Copie la valeur du bit {} sur les bits {} à 31 du registre de destination</li>\n".format(7 if self.byte else 15, 8 if self.byte else 16)
            
            self._readmem = set(range(realAddr, realAddr+sizeaccess))
            self._writeregs |= utils.registerWithCurrentBank(self.rd, bank)

            if self.rd == simulatorContext.PC:
                m = simulatorContext.mem.get(realAddr, size=sizeaccess, mayTriggerBkpt=False)
                if m is not None:
                    res = struct.unpack("<B" if self.byte else "<H", m)[0]
                    self._nextInstrAddr = res

        else:       # STR
            descRange = " de l'octet le moins significatif" if self.byte else " des 2 octets les moins significatifs"
            if self.pre:
                description += descoffset
                description += "<li>Copie la valeur" + descRange + " registre {} dans la mémoire, à l'adresse obtenue à l'étape précédente (pré-incrément), sur {} (STR)</li>\n".format(utils.regSuffixWithBank(self.rd, bank), sizedesc)
            else:
                description += "<li>Copie la valeur" + descRange + " registre {} dans la mémoire, à l'adresse de base, sur {} (STR)</li>\n".format(utils.regSuffixWithBank(self.rd, bank), sizedesc)
                description += descoffset

            self._writemem = set(range(realAddr, realAddr+sizeaccess))
            self._readregs |= utils.registerWithCurrentBank(self.rd, bank)

        if self.pre:
            if self.imm:
                if self.offsetImm == 0:
                    disassembly += "]"
                else:
                    disassembly += ", {}]".format(hex(self.sign * self.offsetImm))
            else:
                disassembly += ", R{}".format(self.offsetReg) + "]"
        else:
            # Post (a post-incrementation of 0 is useless)
            disassembly += "]"
            if self.imm and self.offsetImm != 0:
                disassembly += ", {}".format(hex(self.sign * self.offsetImm))
            elif not self.imm:
                disassembly += ", R{}".format(self.offsetReg)
        #else:
            # Weird case, would happen if we combine post-incrementation and immediate offset of 0
        #    disassembly += "]"

        if self.writeback:
            self._writeregs |= utils.registerWithCurrentBank(self.basereg, bank)
            description += "<li>Écrit l'adresse effective dans le registre de base {} (mode writeback)</li>\n".format(utils.regSuffixWithBank(self.basereg, bank))
            if self.pre:
                disassembly += "!"

        description += "</ol>"

        simulatorContext.regs.reactivateBreakpoints()
        return disassembly, description
    

    def execute(self, simulatorContext):
        if not self._checkCondition(simulatorContext.regs):
            # Nothing to do, instruction not executed
            self.countExecConditionFalse += 1
            return
        self.countExec += 1

        addr = baseval = simulatorContext.regs[self.basereg]
        if self.imm:
            addr += self.sign * self.offsetImm
        else:
            addr += self.sign * simulatorContext.regs[self.offsetReg]

        realAddr = addr if self.pre else baseval
        s = 1 if self.byte else 2
        if self.mode == 'LDR':
            m = simulatorContext.mem.get(realAddr, size=s)
            if m is None:       # No such address in the mapped memory, we cannot continue
                raise ExecutionException("Tentative de lecture de {} octets à partir de l'adresse {} invalide : mémoire non initialisée".format(s, realAddr))
            res = struct.unpack("<B" if self.byte else "<H", m)[0]

            simulatorContext.regs[self.rd] = res
            if self.signed:
                simulatorContext.regs[self.rd] |= 0xFFFFFF00 * ((res >> 7) & 1) if self.byte else 0xFFFF0000 * ((res >> 15) & 1)
                
            if self.rd == simulatorContext.PC:
                self.pcmodified = True
        else:       # STR
            valWrite = simulatorContext.regs[self.rd]
            if self.rd == simulatorContext.PC and simulatorContext.PCSpecialBehavior:
                valWrite += 4       # Special case for PC (see ARM datasheet, 4.9.4)
            valWrite &= 0xFFFF
            simulatorContext.mem.set(realAddr, valWrite, size=1 if self.byte else 2)

        if self.writeback:
            simulatorContext.regs[self.basereg] = addr
