To add a new charm to the list simply add the key to the yaml file and rerun
the script.  It takes a long time and for older charm releases there will be
auth errors with the charm store.  If that happens add a last_release key to the
charm experiencing the problem and set the value to the release where the
problem starts.

If you are doing large queries you may need to provide a GIT_USERNAME and 
GIT_TOKEN for the script

GITHUB_USER=xxx GITHUB_TOKEN=xxx python3 get-charm-revisions.py
