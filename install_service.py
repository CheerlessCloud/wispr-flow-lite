#!/usr/bin/env python3
"""
WisprFlow Lite - Systemd Service Installer
==========================================

This script automatically configures a systemd user service for WisprFlow Lite
voice transcription app with proper PipeWire support.

Features:
- Auto-detects Python virtual environment and script paths
- Configures PipeWire dependencies and environment variables
- Sets up proper user permissions and groups
- Provides service management commands
- Supports both X11 and Wayland sessions
- Cross-platform Linux distribution support

Usage:
    python3 install_service.py install    # Install and enable the service
    python3 install_service.py uninstall  # Remove the service
    python3 install_service.py status     # Check service status
    python3 install_service.py logs       # View service logs
"""

import os
import sys
import subprocess
import pwd
import grp
from pathlib import Path
import shutil
import argparse

class ServiceInstaller:
    def __init__(self):
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.user_id = os.getuid()
        self.home_dir = Path.home()
        self.service_name = "voice-transcriber"
        self.service_file = f"{self.service_name}.service"
        self.systemd_dir = self.home_dir / ".config" / "systemd" / "user"
        self.service_path = self.systemd_dir / self.service_file
        
        # Auto-detect project paths
        self.project_dir = Path(__file__).parent.absolute()
        self.script_path = self.project_dir / "voice_transcriber.py"
        self.venv_path = self.project_dir / ".venv"
        self.python_path = self.venv_path / "bin" / "python"
        
        # Fallback to system Python if venv doesn't exist
        if not self.python_path.exists():
            self.python_path = shutil.which("python3")
        
        print(f"🔍 Detected configuration:")
        print(f"   User: {self.user} (UID: {self.user_id})")
        print(f"   Project: {self.project_dir}")
        print(f"   Python: {self.python_path}")
        print(f"   Script: {self.script_path}")
        print(f"   Service: {self.service_path}")
        print()

    def check_requirements(self):
        """Check if all requirements are met"""
        print("🔧 Checking system requirements...")
        
        # Check if script exists
        if not self.script_path.exists():
            print(f"❌ Script not found: {self.script_path}")
            return False
        
        # Check if Python executable exists
        if not Path(self.python_path).exists():
            print(f"❌ Python not found: {self.python_path}")
            return False
        
        # Check if systemd is available
        if not shutil.which("systemctl"):
            print("❌ systemctl not found - systemd is required")
            return False
        
        # Check PipeWire services
        pipewire_services = ["pipewire.service", "pipewire-pulse.service", "wireplumber.service"]
        missing_services = []
        
        for service in pipewire_services:
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "show", service, "--property=LoadState"],
                    capture_output=True, text=True, check=False
                )
                if "LoadState=loaded" not in result.stdout:
                    missing_services.append(service)
            except Exception:
                missing_services.append(service)
        
        if missing_services:
            print(f"⚠️  PipeWire services not available: {', '.join(missing_services)}")
            print("   This is normal if PipeWire isn't installed. Service will work with PulseAudio too.")
        else:
            print("✅ PipeWire services detected")
        
        print("✅ Requirements check completed")
        return True

    def check_user_groups(self):
        """Check user groups - audio group not needed with modern PipeWire/systemd-logind"""
        print("👥 Checking user group memberships...")
        
        # Modern Linux with PipeWire + systemd-logind doesn't need audio group
        # systemd-logind automatically grants audio permissions via ACLs
        optional_groups = ["rtkit", "input"]
        
        current_groups = [grp.getgrgid(gid).gr_name for gid in os.getgroups()]
        missing_optional = [group for group in optional_groups if group not in current_groups]
        
        # Check if user is in audio group (legacy, should be avoided)
        if "audio" in current_groups:
            print("⚠️  User is in 'audio' group - this is legacy and unnecessary with PipeWire")
            print("   Modern Ubuntu (23.04+) uses systemd-logind for audio permissions")
            print("   Consider removing: sudo gpasswd -d $USER audio")
        
        if missing_optional:
            print(f"⚠️  Missing optional groups: {', '.join(missing_optional)}")
            print("   For better performance, consider adding:")
            print(f"   sudo usermod -a -G {','.join(missing_optional)} {self.user}")
        else:
            print("✅ User group configuration looks good")
        
        # Always return True - audio group not required with modern systems
        print("✅ Audio access via systemd-logind + PipeWire (no audio group needed)")
        return True

    def detect_session_type(self):
        """Detect current session type and set appropriate environment variables"""
        session_type = os.environ.get('XDG_SESSION_TYPE', 'unknown')
        display = os.environ.get('DISPLAY', ':0')
        wayland_display = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')
        xauthority = os.environ.get('XAUTHORITY', '')
        
        print(f"🖥️  Session type: {session_type}")
        
        env_vars = {
            'XDG_RUNTIME_DIR': f'/run/user/{self.user_id}',
            'PIPEWIRE_RUNTIME_DIR': f'/run/user/{self.user_id}/pipewire-0',
            'PULSE_RUNTIME_PATH': f'/run/user/{self.user_id}/pulse',
            'DISPLAY': display,
            'WAYLAND_DISPLAY': wayland_display,
        }
        
        # Set XAUTHORITY appropriately
        if session_type == 'wayland' and not xauthority:
            # For Wayland sessions, XAUTHORITY might be in a different location
            env_vars['XAUTHORITY'] = f'/run/user/{self.user_id}/.mutter-Xwaylandauth.XXXXXX'
        elif xauthority:
            env_vars['XAUTHORITY'] = xauthority
        
        return env_vars

    def generate_service_content(self):
        """Generate the systemd service file content"""
        env_vars = self.detect_session_type()
        
        # Format environment variables for systemd
        env_lines = []
        for key, value in env_vars.items():
            env_lines.append(f"Environment={key}={value}")
        
        service_content = f"""[Unit]
Description=WisprFlow Lite Voice Transcription Service
Documentation=https://github.com/your-repo/wispr-flow-lite
After=graphical-session.target
After=pipewire.service
After=pipewire-pulse.service
After=wireplumber.service
Wants=pipewire.service
Wants=pipewire-pulse.service
Wants=wireplumber.service
PartOf=graphical-session.target
StartLimitBurst=3
StartLimitIntervalSec=60

[Service]
Type=simple
ExecStart={self.python_path} {self.script_path}
Restart=on-failure
RestartSec=5
{chr(10).join(env_lines)}

# Working directory
WorkingDirectory={self.project_dir}

# Security settings
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
MemoryDenyWriteExecute=false

# Audio access handled by systemd-logind + PipeWire (no groups needed)

# Resource limits
MemoryMax=512M
CPUQuota=50%

[Install]
WantedBy=graphical-session.target
"""
        return service_content

    def install_service(self):
        """Install and enable the systemd service"""
        print("📦 Installing systemd user service...")
        
        # Create systemd user directory
        self.systemd_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate and write service file
        service_content = self.generate_service_content()
        with open(self.service_path, 'w') as f:
            f.write(service_content)
        
        print(f"✅ Service file created: {self.service_path}")
        
        # Reload systemd daemon
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        print("✅ Systemd daemon reloaded")
        
        # Import environment variables
        env_vars = ["DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"]
        for var in env_vars:
            if var in os.environ:
                subprocess.run(
                    ["systemctl", "--user", "import-environment", var],
                    check=False  # Don't fail if import fails
                )
        print("✅ Environment variables imported")
        
        # Enable service
        subprocess.run(["systemctl", "--user", "enable", self.service_file], check=True)
        print("✅ Service enabled for autostart")
        
        # Start service immediately
        try:
            subprocess.run(["systemctl", "--user", "start", self.service_name], check=True)
            print("✅ Service started successfully")
            
            # Give it a moment to start and check status
            import time
            time.sleep(2)
            
            result = subprocess.run(
                ["systemctl", "--user", "is-active", self.service_name],
                capture_output=True, text=True, check=False
            )
            
            if result.stdout.strip() == "active":
                print("🎯 Voice transcriber is now running! Hold Alt/Option to record.")
            else:
                print("⚠️  Service started but may not be fully active yet")
                print("   Check status with: python3 install_service.py status")
                
        except subprocess.CalledProcessError as e:
            print(f"⚠️  Service enabled but failed to start: {e}")
            print("   Try starting manually: systemctl --user start voice-transcriber")
            print("   Check logs with: python3 install_service.py logs")
        
        print()
        print("🎉 Installation completed successfully!")
        print()
        print("📋 Service management commands:")
        print(f"   Stop:    systemctl --user stop {self.service_name}")
        print(f"   Restart: systemctl --user restart {self.service_name}")
        print(f"   Status:  systemctl --user status {self.service_name}")
        print(f"   Logs:    journalctl --user -u {self.service_name} -f")
        print(f"   Disable: systemctl --user disable {self.service_name}")
        print()
        print("🔌 The service will restart automatically on next login.")

    def uninstall_service(self):
        """Uninstall and disable the systemd service"""
        print("🗑️  Uninstalling systemd user service...")
        
        # Stop service if running
        subprocess.run(
            ["systemctl", "--user", "stop", self.service_name],
            check=False  # Don't fail if service isn't running
        )
        
        # Disable service
        subprocess.run(
            ["systemctl", "--user", "disable", self.service_name],
            check=False  # Don't fail if service isn't enabled
        )
        
        # Remove service file
        if self.service_path.exists():
            self.service_path.unlink()
            print(f"✅ Service file removed: {self.service_path}")
        
        # Reload daemon
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        print("✅ Systemd daemon reloaded")
        
        print("🎉 Service uninstalled successfully!")

    def show_status(self):
        """Show service status"""
        print(f"📊 Service status for {self.service_name}:")
        print()
        subprocess.run(["systemctl", "--user", "status", self.service_name])

    def show_logs(self):
        """Show service logs"""
        print(f"📜 Recent logs for {self.service_name}:")
        print("Press Ctrl+C to exit log viewing")
        print()
        subprocess.run(["journalctl", "--user", "-u", self.service_name, "-f"])

def main():
    parser = argparse.ArgumentParser(
        description="WisprFlow Lite Systemd Service Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "action",
        choices=["install", "uninstall", "status", "logs"],
        help="Action to perform"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force installation even if requirements check fails"
    )
    
    args = parser.parse_args()
    
    installer = ServiceInstaller()
    
    try:
        if args.action == "install":
            print("🚀 Installing WisprFlow Lite systemd service...")
            print("=" * 50)
            
            if not installer.check_requirements() and not args.force:
                print("❌ Requirements check failed. Use --force to override.")
                sys.exit(1)
            
            if not installer.check_user_groups() and not args.force:
                print("❌ User group check failed. Add user to required groups first.")
                sys.exit(1)
            
            installer.install_service()
            
        elif args.action == "uninstall":
            installer.uninstall_service()
            
        elif args.action == "status":
            installer.show_status()
            
        elif args.action == "logs":
            installer.show_logs()
            
    except subprocess.CalledProcessError as e:
        print(f"❌ Command failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n👋 Operation cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()