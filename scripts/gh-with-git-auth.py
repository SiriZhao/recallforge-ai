"""Run gh using the existing Git credential manager, only in child memory.

Does not update gh's credential store, print tokens, or write a credential file.
"""
import os
import subprocess
import sys


def main():
    credentials=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',
        text=True,capture_output=True)
    values=dict(line.split('=',1) for line in credentials.stdout.splitlines() if '=' in line)
    if credentials.returncode or not values.get('password'):
        raise SystemExit('GitHub Git credential unavailable; no credential content was logged')
    auth_env=os.environ.copy();auth_env['GH_TOKEN']=values['password']
    result=subprocess.run(['gh',*sys.argv[1:]],env=auth_env)
    raise SystemExit(result.returncode)


if __name__=='__main__':main()
