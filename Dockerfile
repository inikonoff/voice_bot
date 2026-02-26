 creating build/temp.linux-x86_64-cpython-311/cbits/webrtc/common_audio/signal_processing
#10 13.04       creating build/temp.linux-x86_64-cpython-311/cbits/webrtc/common_audio/vad
#10 13.04       gcc -Wsign-compare -DNDEBUG -g -fwrapv -O3 -Wall -fPIC -DWEBRTC_POSIX -Icbits -I/usr/local/include/python3.11 -c cbits/pywebrtcvad.c -o build/temp.linux-x86_64-cpython-311/cbits/pywebrtcvad.o
#10 13.04       In file included from cbits/pywebrtcvad.c:1:
#10 13.04       /usr/local/include/python3.11/Python.h:23:12: fatal error: stdlib.h: No such file or directory
#10 13.04          23 | #  include <stdlib.h>
#10 13.04             |            ^~~~~~~~~~
#10 13.04       compilation terminated.
#10 13.04       error: command '/usr/bin/gcc' failed with exit code 1
#10 13.04       [end of output]
#10 13.04   
#10 13.04   note: This error originates from a subprocess, and is likely not a problem with pip.
#10 13.04   ERROR: Failed building wheel for webrtcvad
#10 13.04 Failed to build webrtcvad
#10 13.10 error: failed-wheel-build-for-install
#10 13.10 
#10 13.10 × Failed to build installable wheels for some pyproject.toml based projects
#10 13.10 ╰─> webrtcvad
#10 ERROR: process "/bin/sh -c pip install --no-cache-dir --upgrade pip     && pip install --no-cache-dir -r requirements.txt" did not complete successfully: exit code: 1
------
 > [ 5/11] RUN pip install --no-cache-dir --upgrade pip     && pip install --no-cache-dir -r requirements.txt:
13.04       error: command '/usr/bin/gcc' failed with exit code 1
13.04       [end of output]
13.04   
13.04   note: This error originates from a subprocess, and is likely not a problem with pip.
13.04   ERROR: Failed building wheel for webrtcvad
13.04 Failed to build webrtcvad
13.10 error: failed-wheel-build-for-install
13.10 
13.10 × Failed to build installable wheels for some pyproject.toml based projects
13.10 ╰─> webrtcvad
------
Dockerfile:37
--------------------
  36 |     
  37 | >>> RUN pip install --no-cache-dir --upgrade pip \
  38 | >>>     && pip install --no-cache-dir -r requirements.txt
  39 |     
--------------------
error: failed to solve: process "/bin/sh -c pip install --no-cache-dir --upgrade pip     && pip install --no-cache-dir -r requirements.txt" did not complete successfully: exit code: 1
error: exit status 1
