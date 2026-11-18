import sys
d=open(sys.argv[1],'rb').read()
# ELF section header end = runtime size
import struct
shoff=struct.unpack_from('<Q',d,0x28)[0]; shentsize,shnum=struct.unpack_from('<HH',d,0x3A)
print(shoff+shentsize*shnum)
