import subprocess
import multiprocessing

scripts = ['app.py', 'run_ipv6.py']

def run_script(script):
    subprocess.run(['python3', script])

if __name__ == "__main__":
    for script in scripts:
        print(script)
        multiprocessing.Process(target=run_script, args=(script,)).start()