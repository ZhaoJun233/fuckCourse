from Crypto.Cipher import AES
from base64 import b64encode, b64decode
from ObjDict import ObjDict
import json

IV = b"1g3qqdh4jvbskb9x"

HOME_KEY  = b"7q9oko0vqb3la20r"
AI_KEY=b"hw2fdlwcj4cs1mx7"
VIDEO_KEY = b"azp53h0kft7qi78q"
QA_KEY    = b"kcGOlISPkYKRksSK"
EXAM_KEY  = b"onbfhdyvz8x7otrp"

class Cipher:
    def __init__(self, key:bytes=VIDEO_KEY, iv:bytes=IV):
        self.key = key
        self.iv = iv

    @staticmethod
    def pad(data):
        return (data+chr(16-len(data)%16)*(16-len(data)%16)).encode()
    
    @staticmethod
    def unpad(data):
        data = data.decode()
        return data[:-ord(data[-1])]

    def encrypt(self, data:str):
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return b64encode(cipher.encrypt(Cipher.pad(data))).decode()

    def decrypt(self, data:str):
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return Cipher.unpad(cipher.decrypt(b64decode(data)))

class WatchPoint:
    def __init__(self, init:int=0):
        self.reset(init)

    def add(self, end:int, start:int=None):
        wp_interval = 2 # watch point record interval in seconds
        start = self.last if start is None else start
        end = int(end)
        self.last = end
        for i in range(start, end+1)[::wp_interval]:
            self.wp.append(self.gen(i))

    def get(self):
        return ','.join(map(str,self.wp))

    def reset(self, init:int=0):
        self.wp = [0,1]
        self.last = int(init) or 1
    
    @staticmethod
    def gen(time:int):
        return int(time//5+2)

def getEv(data:list, key:str="zzpttjd"):
    """
    * key:
      * d26666 -> "zzpttjd" (default)
      * d24444 -> "zhihuishu"
    """
    def gen():
        while True:
            for c in key:
                yield ord(c)
    gen = gen()
    data = ';'.join(map(str, data))
    ev = ""
    for c in data:
        tmp = hex(ord(c)^next(gen)).replace("0x", "")
        if len(tmp)<2:
            tmp = '0' + tmp
        ev += tmp[-4:] # actually -2 is fine, but their sauce code said -4
    return ev

if __name__ == "__main__":
    # Local round-trip example with synthetic data only.
    cipher = Cipher()
    sample = {"courseId": "example-course", "classId": 0, "dateFormate": 0}
    encoded = cipher.encrypt(json.dumps(sample))
    print(encoded)
    print(cipher.decrypt(encoded))