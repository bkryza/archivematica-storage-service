from __future__ import absolute_import
from __future__ import print_function
import hashlib
import os


"""
- Update django secret key
- syncdb
- collectstatic
- fix perms
- restart nginx and uwsgi
"""


def gen():
    return hashlib.sha1(os.urandom(512)).hexdigest()


myhash = gen()

print(myhash)
