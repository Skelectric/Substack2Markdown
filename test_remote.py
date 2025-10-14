#!/usr/bin/env python3
"""
Test script for remote Substack scraper connection
This script tests SSH access to the miniPC server
"""

import subprocess
import sys
from pathlib import Path

def test_connection():
    """Test SSH connection to miniPC"""
    print("Testing connection to miniPC server...")
    print(f"Target: ubuntu@192.168.104.209")
    
    try:
        result = subprocess.run([
            "ssh", 
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "ubuntu@192.168.104.209",
            "echo 'Connection successful'"
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            print("✓ Successfully connected to miniPC server")
            print(f"Response: {result.stdout.strip()}")
            return True
        else:
            print(f"✗ Connection failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("✗ Connection timed out")
        return False
    except Exception as e:
        print(f"✗ Connection error: {e}")
        return False

def test_remote_directories():
    """Test if remote directories exist and are accessible"""
    print("\nTesting remote directory access...")
    
    try:
        result = subprocess.run([
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "ubuntu@192.168.104.209",
            "ls -la /home/ubuntu/substacks"
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            print("✓ Remote substacks directory is accessible")
            print("Directory contents:")
            print(result.stdout)
            return True
        else:
            print(f"✗ Remote directory access failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"✗ Directory test error: {e}")
        return False

def main():
    print("Substack Remote Scraper Connection Test")
    print("=" * 50)
    
    # Test basic connection
    connection_ok = test_connection()
    
    if connection_ok:
        # Test directory access
        directory_ok = test_remote_directories()
        
        if directory_ok:
            print("\n✓ All tests passed! The scraper should work correctly.")
            sys.exit(0)
        else:
            print("\n⚠ Connection works but directory access failed.")
            print("The scraper will fall back to local storage.")
            sys.exit(1)
    else:
        print("\n✗ Connection test failed.")
        print("Please ensure:")
        print("1. The miniPC server is running at 192.168.104.209")
        print("2. SSH is enabled and accessible")
        print("3. Your SSH key is properly configured")
        sys.exit(1)

if __name__ == "__main__":
    main()
