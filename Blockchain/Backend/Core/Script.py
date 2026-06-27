from Blockchain.Backend.util.util import encode_varient, int_to_little_endian
from Blockchain.Backend.Core.EllepticCurve.op import OP_CODE_FUNCTION

class script:
    def __init__(self, cmds=None):
        if cmds is None:
            self.cmds =[]
        else:
            self.cmds = cmds

    def __add__(self, other):
        return script(self.cmds + other.cmds)

    def serialize(self):
        result = b''
        for cmd in self.cmds:
            if type(cmd) == int:
                result += int_to_little_endian(cmd, 1)
            
            else:
                length = len(cmd)
                if length <= 75:
                    result += int_to_little_endian(length, 1)
                elif length > 75 and length < 0x100:
                    result += int_to_little_endian(76, 1)
                    result += int_to_little_endian(length, 1)
                elif length >= 0x100 and length < 520:
                    result += int_to_little_endian(77, 1)
                    result += int_to_little_endian(length, 1)
                else:
                    raise ValueError("Too long for CMD")
                
                result += cmd

        total = len(result)
        return encode_varient(total)+result
    
    def evaluate(self, z):
        # cmds = self.cmds
        cmds = self.cmds[:]
        stack = []

        while len(cmds)>0:
            cmd = cmds.pop(0)

            if type(cmd) == int:
                operation = OP_CODE_FUNCTION[cmd]

                if cmd == 172:
                    if not operation(stack, z):
                        print(f"Error in Signature Verification")
                        return False
                    
                elif not operation(stack):
                    print(f"Error in Signature Verification")
                    return False

            else:
                stack.append(cmd)   

        return True

    @classmethod
    def p2pkh_script(cls, h160):
        """Takes a hash160 and return Peer2Publick Key Hash Script Key"""
        return script([0x76, 0xa9, h160, 0x88, 0xac])
    
    