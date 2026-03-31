import os
import sys
import socket
import subprocess
import tempfile

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
import django
django.setup()

from autotest.services.browser_use_runner import BrowserUseRunner

# Create a test execution
from autotest.models import AutoTestExecution
from testcases.models import TestCase
from users.models import User

user = User.objects.filter(username='admin').first()
if not user:
    print("No admin user found!")
    sys.exit(1)

case = TestCase.objects.filter(creator=user).first()
if not case:
    print("No test case found!")
    sys.exit(1)

# Clean up old executions
AutoTestExecution.objects.filter(case=case).delete()

execution = AutoTestExecution.objects.create(
    case=case,
    executor=user,
    trigger_user=user,
    status='pending',
    trigger_reason='manual_test'
)

print(f"Created execution ID: {execution.id}")

# Try to launch browser
runner = BrowserUseRunner(execution.id)

try:
    chrome_path = runner._find_chrome_executable()
    print(f"Chrome executable: {chrome_path}")
    
    if not chrome_path:
        print("ERROR: Could not find Chrome executable!")
        sys.exit(1)
    
    port = runner._get_free_port()
    print(f"Using port: {port}")
    
    user_data_dir = tempfile.mkdtemp()
    print(f"User data dir: {user_data_dir}")
    
    headless = True
    use_new_headless = True
    
    cmd = [
        chrome_path,
        f'--remote-debugging-port={port}',
        '--remote-debugging-address=127.0.0.1',
        '--no-sandbox',
        '--disable-gpu',
        ('--headless=new' if use_new_headless else '--headless') if headless else '',
        '--window-size=1440,900',
        '--force-device-scale-factor=1',
        f'--user-data-dir={user_data_dir}',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-dev-shm-usage',
        '--disable-software-rasterizer',
    ]
    cmd = [x for x in cmd if x]
    
    print(f"Launching: {' '.join(cmd)}")
    
    proc = subprocess.Popen(cmd)
    print(f"Process started with PID: {proc.pid}")
    
    # Wait for CDP to be ready
    import time
    import urllib.request
    import urllib.error
    
    cdp_url = f"http://127.0.0.1:{port}"
    print(f"Waiting for CDP to be ready at {cdp_url}...")
    
    for i in range(30):
        time.sleep(0.5)
        try:
            req = urllib.request.Request(f"{cdp_url}/json/version")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    print(f"✅ CDP is ready! Response: {resp.read().decode()[:200]}")
                    break
        except Exception as e:
            if i % 10 == 0:
                print(f"  Waiting... ({i*0.5}s) - {e}")
            continue
    else:
        print("❌ ERROR: CDP did not become ready within 15 seconds")
        proc.kill()
        sys.exit(1)
    
    # Clean up
    print("Terminating browser...")
    proc.terminate()
    proc.wait(timeout=5)
    print("✅ Test completed successfully!")
    
except Exception as e:
    import traceback
    print(f"❌ ERROR: {e}")
    traceback.print_exc()
    sys.exit(1)
