import subprocess
import multiprocessing

scripts = ['app.py', 'ipv6.py']

def run_script(script):
    subprocess.run(['python3', script])

if __name__ == "__main__":
    for script in scripts:
        multiprocessing.Process(target=run_script, args=(script,)).start()