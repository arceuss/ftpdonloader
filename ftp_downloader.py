#!/usr/bin/env python3
"""
Multi-threaded GUI FTP Downloader
A GUI application for downloading files from FTP servers with timestamp preservation
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import subprocess
import os
import sys
import threading
import queue
import time
import shutil
import re
import ftplib
import ftputil
from pathlib import Path
from urllib.parse import quote


def create_no_utf8_session_factory(base_class, port=21, use_passive_mode=True, encrypt_data_channel=False):
    """Create a session factory that doesn't send OPTS UTF8 ON command"""
    def session_factory(host, username, password):
        # Create the base FTP connection manually without UTF8 command
        session = base_class()
        session.connect(host, port)
        if username or password:
            session.login(username, password)
        if use_passive_mode:
            session.set_pasv(True)
        if encrypt_data_channel and hasattr(session, 'prot_p'):
            session.prot_p()
        return session
    
    return session_factory


class DownloadWorker(threading.Thread):
    """Worker thread for downloading files using ftputil (preserves timestamps)"""
    def __init__(self, worker_id, download_queue, stats, host, port, 
                 local_dir, progress_callback, status_callback, username='', password='', use_tls=False, remote_base='/'):
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.download_queue = download_queue
        self.stats = stats
        self.host = host
        self.port = port
        self.local_dir = local_dir
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.remote_base = remote_base
        self.running = True
        self.ftp_host = None
        self.downloaded_paths = set()  # Track downloaded paths to avoid duplicates
        
    def run(self):
        """Main worker loop - pulls files from queue and downloads them"""
        # Connect to FTP server once per worker using ftputil
        try:
            # Create session factory that doesn't send UTF8 command
            if self.use_tls:
                session_factory = create_no_utf8_session_factory(
                    base_class=ftplib.FTP_TLS,
                    port=self.port,
                    use_passive_mode=True,
                    encrypt_data_channel=True
                )
            else:
                session_factory = create_no_utf8_session_factory(
                    base_class=ftplib.FTP,
                    port=self.port,
                    use_passive_mode=True,
                    encrypt_data_channel=False
                )
            
            # Create FTPHost with custom session factory (no UTF8 command will be sent)
            self.ftp_host = ftputil.FTPHost(self.host, self.username, self.password,
                                           session_factory=session_factory)
            
            # Synchronize times for accurate timestamp preservation
            try:
                self.ftp_host.synchronize_times()
            except Exception:
                pass  # Continue even if time sync fails
                
        except Exception as e:
            error_msg = f"Worker {self.worker_id} connection failed: {str(e)}"
            with self.stats['lock']:
                self.stats['errors'].append(error_msg)
            # Log to console/stderr so user can see the error
            import sys
            print(f"ERROR: {error_msg}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return
        
        # Pull tasks from queue and download
        while self.running:
            try:
                # Get task from queue (with timeout to check running flag)
                task = self.download_queue.get(timeout=1)
                if task is None:  # Poison pill to stop
                    break
                
                remote_path, local_path = task
                
                # Check if file already exists locally
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    # File already exists, skip download
                    with self.stats['lock']:
                        if 'downloaded_paths' not in self.stats:
                            self.stats['downloaded_paths'] = set()
                        if remote_path not in self.stats['downloaded_paths']:
                            self.stats['downloaded_paths'].add(remote_path)
                            # Don't increment total here - it was already counted when discovered
                            self.stats['completed'] += 1
                            self.stats['success'] += 1
                    if self.status_callback:
                        self.status_callback(remote_path, "Completed")
                    self.download_queue.task_done()
                    continue
                
                # Check if already downloaded or currently being downloaded (race condition protection)
                with self.stats['lock']:
                    if 'downloaded_paths' not in self.stats:
                        self.stats['downloaded_paths'] = set()
                    if 'downloading_paths' not in self.stats:
                        self.stats['downloading_paths'] = set()
                    if 'queued_files' not in self.stats:
                        self.stats['queued_files'] = 0
                    
                    # Skip if already downloaded or currently downloading
                    if remote_path in self.stats['downloaded_paths']:
                        self.download_queue.task_done()
                        continue
                    if remote_path in self.stats['downloading_paths']:
                        self.download_queue.task_done()
                        continue
                    
                    # Mark as downloading (file is now being processed)
                    # Note: We don't decrement queued_files here because queue.qsize() is more accurate
                    self.stats['downloading_paths'].add(remote_path)
                
                try:
                    # Notify that download is starting
                    if self.status_callback:
                        self.status_callback(remote_path, "Downloading...")
                    
                    # Create local directory if needed
                    local_dir = os.path.dirname(local_path)
                    if local_dir:
                        os.makedirs(local_dir, exist_ok=True)
                    
                    # Download file using FTP
                    self._download_file(remote_path, local_path)
                    
                    # Update stats - mark as downloaded and remove from downloading
                    with self.stats['lock']:
                        self.stats['downloading_paths'].discard(remote_path)
                        self.stats['downloaded_paths'].add(remote_path)
                        self.stats['completed'] += 1
                        self.stats['success'] += 1
                    
                    # Notify that download completed
                    if self.status_callback:
                        self.status_callback(remote_path, "Completed")
                    
                except Exception as e:
                    error_msg = str(e)
                    # Check if this is actually a success message being treated as error
                    # "200 TYPE is now 8-bit binary" is a success response, not an error
                    # Some FTP servers return this as part of normal operation
                    if '200' in error_msg and ('TYPE' in error_msg.upper() or 'binary' in error_msg.lower()):
                        # This is actually a success message, treat as completed
                        with self.stats['lock']:
                            self.stats['downloading_paths'].discard(remote_path)
                            self.stats['downloaded_paths'].add(remote_path)
                            self.stats['completed'] += 1
                            self.stats['success'] += 1
                        if self.status_callback:
                            self.status_callback(remote_path, "Completed")
                    else:
                        # Real error - remove from downloading but don't mark as downloaded
                        with self.stats['lock']:
                            self.stats['downloading_paths'].discard(remote_path)
                            self.stats['completed'] += 1
                            self.stats['failed'] += 1
                            self.stats['errors'].append(f"{remote_path}: {error_msg}")
                        
                        # Notify that download failed
                        if self.status_callback:
                            # Show full error message (or at least more of it)
                            self.status_callback(remote_path, f"Failed: {error_msg[:100]}")
                
                finally:
                    self.download_queue.task_done()
                    
            except queue.Empty:
                # Queue is empty, check if we should continue
                continue
            except Exception as e:
                with self.stats['lock']:
                    self.stats['errors'].append(f"Worker {self.worker_id} error: {str(e)}")
        
        # Disconnect when done
        if self.ftp_host:
            try:
                self.ftp_host.close()
            except:
                pass
            self.ftp_host = None
    
    def _download_recursive(self, current_path, base_path):
        """Recursively download all files from a directory"""
        if not self.running:
            return
        
        try:
            # Change to current directory
            if current_path != '/':
                self.ftp.cwd(current_path)
            else:
                self.ftp.cwd('/')
            
            # List directory contents
            items = []
            try:
                # Try MLSD first (more reliable)
                for item in self.ftp.mlsd():
                    items.append(item)
            except:
                # Fallback to LIST
                lines = []
                self.ftp.retrlines('LIST', lines.append)
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 9:
                        name = ' '.join(parts[8:])
                        is_dir = parts[0].startswith('d')
                        size = parts[4] if len(parts) > 4 else 'Unknown'
                        items.append((name, {'type': 'dir' if is_dir else 'file', 'size': size}))
            
            # Separate directories and files to ensure we process ALL files
            # Process files first, then directories
            dirs = []
            files = []
            
            for name, info in items:
                if name in ['.', '..']:
                    continue
                
                # Build proper path preserving structure
                if current_path == '/':
                    remote_path = f"/{name}"
                else:
                    remote_path = f"{current_path.rstrip('/')}/{name}"
                remote_path = remote_path.replace('\\', '/')
                
                if info.get('type') == 'dir':
                    dirs.append((remote_path, info))
                else:
                    files.append((remote_path, info))
            
            # Process files first - this ensures root-level files are downloaded
            for remote_path, info in files:
                if not self.running:
                    break
                
                # Calculate local path - preserve exact 1:1 structure
                if remote_path.startswith('/'):
                    rel_path = remote_path[1:]
                else:
                    rel_path = remote_path
                
                local_path = os.path.join(self.local_dir, rel_path)
                
                # Skip if already downloaded (avoid duplicates across workers)
                with self.stats['lock']:
                    if remote_path in self.stats.get('downloaded_paths', set()):
                        continue
                    if 'downloaded_paths' not in self.stats:
                        self.stats['downloaded_paths'] = set()
                    self.stats['downloaded_paths'].add(remote_path)
                    # Don't increment total here - it was already counted when discovered
                
                try:
                    # Notify that download is starting
                    if self.status_callback:
                        self.status_callback(remote_path, "Downloading...")
                    
                    # Create local directory if needed
                    local_dir = os.path.dirname(local_path)
                    if local_dir:
                        os.makedirs(local_dir, exist_ok=True)
                    
                    # Download file using FTP
                    self._download_file(remote_path, local_path)
                    
                    # Update stats
                    with self.stats['lock']:
                        self.stats['completed'] += 1
                        self.stats['success'] += 1
                    
                    # Notify that download completed
                    if self.status_callback:
                        self.status_callback(remote_path, "Completed")
                    
                except Exception as e:
                    # Update stats
                    with self.stats['lock']:
                        self.stats['completed'] += 1
                        self.stats['failed'] += 1
                        self.stats['errors'].append(f"{remote_path}: {str(e)}")
                    
                    # Notify that download failed
                    if self.status_callback:
                        # Show full error message (or at least more of it)
                        self.status_callback(remote_path, f"Failed: {str(e)[:100]}")
            
            # Then recursively process directories
            for remote_path, info in dirs:
                if not self.running:
                    break
                # Recursively download subdirectory
                self._download_recursive(remote_path, base_path)
                            
        except Exception as e:
            with self.stats['lock']:
                self.stats['errors'].append(f"Error in {current_path}: {str(e)}")
    
    def _download_file(self, remote_path, local_path):
        """Download a single file using ftputil (preserves timestamps)"""
        # ftputil works with paths relative to current directory or absolute paths
        # Normalize the path - try both absolute (with /) and relative (without /)
        remote_path_normalized = remote_path
        if not remote_path.startswith('/'):
            remote_path_normalized = '/' + remote_path
        
        remote_path_alt = remote_path.lstrip('/')
        
        # Get file size for progress tracking
        file_size = None
        working_path = None
        for try_path in [remote_path_normalized, remote_path_alt]:
            try:
                file_size = self.ftp_host.path.getsize(try_path)
                working_path = try_path
                break
            except Exception:
                continue

        downloaded = 0
        start_time = time.time()
        last_update_time = start_time
        last_bytes = 0
        chunk_size = 8192  # 8KB chunks for progress updates

        # Download file using ftputil's open() method for progress tracking
        # Try both path formats if needed
        download_succeeded = False
        last_error = None
        
        for try_path in [working_path, remote_path_normalized, remote_path_alt]:
            if try_path is None:
                continue
            try:
                with self.ftp_host.open(try_path, 'rb') as remote_file:
                    with open(local_path, 'wb') as local_file:
                        while True:
                            chunk = remote_file.read(chunk_size)
                            if not chunk:
                                break
                            
                            data_len = len(chunk)
                            local_file.write(chunk)
                            downloaded += data_len
                            current_time = time.time()

                            # Update total bytes downloaded for speed calculation
                            with self.stats['lock']:
                                self.stats['bytes_downloaded'] += data_len

                            # Calculate speed for this file (update every 0.5 seconds)
                            if current_time - last_update_time >= 0.5:
                                elapsed = current_time - last_update_time
                                bytes_since_last = downloaded - last_bytes
                                file_speed = bytes_since_last / elapsed if elapsed > 0 else 0
                                last_update_time = current_time
                                last_bytes = downloaded

                                # Format speed
                                if file_speed >= 1024 * 1024:
                                    speed_str = f"{file_speed / (1024 * 1024):.1f} MB/s"
                                elif file_speed >= 1024:
                                    speed_str = f"{file_speed / 1024:.1f} KB/s"
                                else:
                                    speed_str = f"{file_speed:.0f} B/s"

                                if file_size and self.status_callback:
                                    percent = int((downloaded / file_size) * 100) if file_size else 0
                                    self.status_callback(remote_path, f"Downloading {percent}%", speed_str)
                                if self.progress_callback and file_size:
                                    percent = int((downloaded / file_size) * 100) if file_size else 0
                                    self.progress_callback(self.worker_id, remote_path, percent)
                            else:
                                # Still update status but not speed
                                if file_size and self.status_callback:
                                    percent = int((downloaded / file_size) * 100) if file_size else 0
                                    self.status_callback(remote_path, f"Downloading {percent}%")
                                if self.progress_callback and file_size:
                                    percent = int((downloaded / file_size) * 100) if file_size else 0
                                    self.progress_callback(self.worker_id, remote_path, percent)
                
                # Preserve the modification time from the remote file
                try:
                    # Use the working path that succeeded
                    mtime_path = working_path if working_path else try_path
                    remote_mtime = self.ftp_host.path.getmtime(mtime_path)
                    # Set the modification time on the local file
                    os.utime(local_path, (remote_mtime, remote_mtime))
                except Exception:
                    # If we can't get/set the mtime, continue anyway (file is downloaded)
                    pass
                
                download_succeeded = True
                break  # Success, exit the loop
                
            except Exception as e:
                last_error = e
                continue  # Try next path format
        
        if not download_succeeded:
            error_msg = str(last_error) if last_error else "Unknown error"
            # Check for common FTP error codes and provide better messages
            if '550' in error_msg or 'not found' in error_msg.lower() or 'No such file' in error_msg:
                raise Exception(f"File not found: {error_msg}")
            elif '150' in error_msg:
                # 150 is "opening data connection" - might indicate timeout
                raise Exception(f"Connection timeout or interrupted: {error_msg}")
            else:
                raise Exception(f"FTP error: {error_msg}")
    
    def stop(self):
        """Stop the worker"""
        self.running = False
        if self.ftp_host:
            try:
                self.ftp_host.close()
            except:
                pass


class FTPDownloaderGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("ftp donloader")
        self.root.geometry("900x700")
        
        # Set window icon
        icon_path = os.path.join(os.path.dirname(__file__), "donload.png")
        if os.path.exists(icon_path):
            try:
                # Try using PIL to load PNG and set as icon (works cross-platform)
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(icon_path)
                    photo = ImageTk.PhotoImage(img)
                    self.root.iconphoto(True, photo)  # True = set for all future toplevels too
                    # Keep a reference to prevent garbage collection
                    self.root._icon_photo = photo
                except ImportError:
                    # PIL not available, try direct iconbitmap (may not work with PNG)
                    try:
                        self.root.iconbitmap(icon_path)
                    except:
                        pass  # Icon setting failed, continue without it
            except Exception:
                pass  # Icon setting failed, continue without it
        
        # State variables
        self.download_queue = queue.Queue()
        self.workers = []
        self.stats = {
            'total': 0,
            'completed': 0,
            'success': 0,
            'failed': 0,
            'errors': [],
            'lock': threading.Lock(),
            'bytes_downloaded': 0,  # Total bytes downloaded
            'total_size': 0,  # Total size of all files to download (in bytes)
            'download_start_time': None,  # When download started
            'last_bytes': 0,  # Bytes at last speed calculation
            'last_speed_time': None,  # Time of last speed calculation
            'current_speed': 0.0  # Current download speed in bytes/sec
        }
        self.is_downloading = False
        self.file_list = []
        self.download_process = None
        self.file_to_item = {}  # Map file paths to tree item IDs
        self.current_downloads = {}  # Track currently downloading files
        self.downloading_items_moved = set()  # Track which items have been moved to top
        self.completed_downloads = []  # Track completed downloads
        self.failed_downloads = []  # Track failed downloads as (remote_path, local_path) tuples
        self.failed_downloads_dict = {}  # Track failed downloads: {remote_path: local_path}
        self.scanner_done = False  # Track if scanners have finished discovering files
        self.scanned_dirs = set()  # Track which directories have been scanned (for parallel scanners)
        self.scanned_dirs_lock = threading.Lock()  # Lock for scanned_dirs set
        self.scanner_count = 0  # Track number of active scanners
        self.scanner_count_lock = threading.Lock()  # Lock for scanner_count
        self.completion_dialog_shown = False  # Prevent showing dialog multiple times
        self.completion_checks_passed = 0  # Track consecutive successful completion checks
        self.all_tree_items = set()  # Track all treeview items for search filtering
        
        # Load status images
        self.status_images = {}
        self.has_pil = False
        images_dir = os.path.join(os.path.dirname(__file__), 'images')
        if os.path.exists(images_dir):
            try:
                from PIL import Image, ImageTk
                self.has_pil = True
                # Load images
                success_img = os.path.join(images_dir, 'success.jpg')
                failed_img = os.path.join(images_dir, 'failed.jpg')
                successwithfails_img = os.path.join(images_dir, 'successwithfails.jpg')
                
                if os.path.exists(success_img):
                    img = Image.open(success_img)
                    img = img.resize((16, 16), Image.Resampling.LANCZOS)
                    self.status_images['success'] = ImageTk.PhotoImage(img)
                if os.path.exists(failed_img):
                    img = Image.open(failed_img)
                    img = img.resize((16, 16), Image.Resampling.LANCZOS)
                    self.status_images['failed'] = ImageTk.PhotoImage(img)
                if os.path.exists(successwithfails_img):
                    img = Image.open(successwithfails_img)
                    img = img.resize((16, 16), Image.Resampling.LANCZOS)
                    self.status_images['successwithfails'] = ImageTk.PhotoImage(img)
            except ImportError:
                self.has_pil = False
            except Exception as e:
                self.has_pil = False
        
        self.setup_ui()
        
    def setup_ui(self):
        """Create the user interface"""
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Connection settings frame
        conn_frame = ttk.LabelFrame(main_frame, text="FTP Connection Settings", padding="10")
        conn_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)
        
        # Host
        ttk.Label(conn_frame, text="Host:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.host_entry = ttk.Entry(conn_frame, width=30)
        self.host_entry.grid(row=0, column=1, padx=5, pady=5)
        self.host_entry.insert(0, "modland.com")
        
        # Port
        ttk.Label(conn_frame, text="Port:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
        self.port_entry = ttk.Entry(conn_frame, width=10)
        self.port_entry.grid(row=0, column=3, padx=5, pady=5)
        self.port_entry.insert(0, "21")
        
        # Username
        ttk.Label(conn_frame, text="Username:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.username_entry = ttk.Entry(conn_frame, width=30)
        self.username_entry.grid(row=1, column=1, padx=5, pady=5)
        self.username_entry.insert(0, "anonymous")
        
        # Password
        ttk.Label(conn_frame, text="Password:").grid(row=1, column=2, sticky=tk.W, padx=5, pady=5)
        self.password_entry = ttk.Entry(conn_frame, width=10, show="*")
        self.password_entry.grid(row=1, column=3, padx=5, pady=5)
        
        # TLS checkbox
        self.use_tls_var = tk.BooleanVar()
        ttk.Checkbutton(conn_frame, text="Use TLS/SSL", 
                       variable=self.use_tls_var).grid(row=2, column=0, padx=5, pady=5)
        
        # Remote path
        ttk.Label(conn_frame, text="Remote Path:").grid(row=2, column=1, sticky=tk.W, padx=5, pady=5)
        self.remote_path_entry = ttk.Entry(conn_frame, width=30)
        self.remote_path_entry.grid(row=2, column=2, columnspan=2, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.remote_path_entry.insert(0, "/")
        
        # Local directory
        ttk.Label(conn_frame, text="Local Directory:").grid(row=3, column=0, sticky=tk.W, padx=5, pady=5)
        self.local_dir_entry = ttk.Entry(conn_frame, width=30)
        self.local_dir_entry.grid(row=3, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.local_dir_entry.insert(0, os.path.join(os.getcwd(), "downloads"))
        
        ttk.Button(conn_frame, text="Browse", 
                  command=self.browse_directory).grid(row=3, column=2, padx=5, pady=5)
        
        # Download settings frame
        settings_frame = ttk.LabelFrame(main_frame, text="Download Settings", padding="10")
        settings_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)
        
        # Number of threads
        ttk.Label(settings_frame, text="Number of Threads:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.threads_var = tk.IntVar(value=4)
        threads_spin = ttk.Spinbox(settings_frame, from_=1, to=20, 
                                   textvariable=self.threads_var, width=10)
        threads_spin.grid(row=0, column=1, padx=5, pady=5)
        
        # Number of scanners
        ttk.Label(settings_frame, text="Number of Scanners:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
        self.scanners_var = tk.IntVar(value=4)
        scanners_spin = ttk.Spinbox(settings_frame, from_=1, to=10, 
                                    textvariable=self.scanners_var, width=10)
        scanners_spin.grid(row=0, column=3, padx=5, pady=5)
        
        # Retry failed downloads checkbox
        self.retry_failed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings_frame, text="Auto-retry failed downloads", 
                       variable=self.retry_failed_var).grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)
        
        
        # Control buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=2, column=0, columnspan=2, pady=10)
        
        self.test_connection_button = ttk.Button(button_frame, text="Test Connection", 
                                                 command=self.test_connection)
        self.test_connection_button.pack(side=tk.LEFT, padx=5)
        
        self.download_button = ttk.Button(button_frame, text="Start Download", 
                                          command=self.start_download)
        self.download_button.pack(side=tk.LEFT, padx=5)
        
        self.stop_button = ttk.Button(button_frame, text="Stop Download", 
                                     command=self.stop_download, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)
        
        # Stats frame (removed progress bar, keeping stats)
        stats_frame = ttk.LabelFrame(main_frame, text="Statistics", padding="10")
        stats_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)
        
        # Stats
        self.stats_var = tk.StringVar(value="Files: 0 | Total Size: Unknown | Progress: N/A | ETA: N/A | Completed: 0 | Pending: 0 | Failed: 0 | Speed: 0 B/s")
        ttk.Label(stats_frame, textvariable=self.stats_var).pack(anchor=tk.W)
        
        # Remove progress_var and progress_bar references - they're no longer needed
        
        # File list
        list_frame = ttk.LabelFrame(main_frame, text="Files to Download", padding="10")
        list_frame.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)
        
        # Search/filter frame
        search_frame = ttk.Frame(list_frame)
        search_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(search_frame, text="Filter:").pack(side=tk.LEFT, padx=5)
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', self._on_search_change)
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        clear_search_button = ttk.Button(search_frame, text="Clear", command=self._clear_search)
        clear_search_button.pack(side=tk.LEFT, padx=5)
        
        # Treeview for file list with scrollbars
        tree_frame = ttk.Frame(list_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        # Add scrollbars
        tree_scrollbar_y = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
        tree_scrollbar_x = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL)
        
        self.tree = ttk.Treeview(tree_frame, columns=("size", "status", "speed"), show="tree headings", height=10,
                                yscrollcommand=tree_scrollbar_y.set, xscrollcommand=tree_scrollbar_x.set)
        self.tree.heading("#0", text="File Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.heading("speed", text="Speed")
        self.tree.column("#0", width=350)
        self.tree.column("size", width=100)
        self.tree.column("status", width=120)
        self.tree.column("speed", width=100)
        
        # Configure scrollbars
        tree_scrollbar_y.config(command=self.tree.yview)
        tree_scrollbar_x.config(command=self.tree.xview)
        
        # Use grid for treeview and scrollbars
        self.tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        tree_scrollbar_y.grid(row=0, column=1, sticky=(tk.N, tk.S))
        tree_scrollbar_x.grid(row=1, column=0, sticky=(tk.W, tk.E))
        
        # Configure grid weights for proper resizing
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Configure tags for status images (if available)
        if self.has_pil:
            if 'success' in self.status_images:
                self.tree.tag_configure('success', image=self.status_images['success'])
            if 'failed' in self.status_images:
                self.tree.tag_configure('failed', image=self.status_images['failed'])
        
        # Right-click context menu for treeview
        self.tree_context_menu = tk.Menu(self.root, tearoff=0)
        self.tree_context_menu.add_command(label="Copy File Path", command=self._copy_selected_path)
        self.tree_context_menu.add_command(label="Open File Location", command=self._open_selected_file_location)
        self.tree_context_menu.add_command(label="Retry Download", command=self._retry_selected_file)
        self.tree.bind("<Button-3>", self._on_treeview_right_click)  # Right-click
        self.tree.bind("<Button-2>", self._on_treeview_right_click)  # Mac right-click
        
        # Completed and Failed downloads frame
        results_frame = ttk.LabelFrame(main_frame, text="Download Results", padding="10")
        results_frame.grid(row=5, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)
        
        # Create a paned window to split completed and failed
        paned = ttk.PanedWindow(results_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)
        
        # Completed downloads listbox
        completed_frame = ttk.Frame(paned)
        paned.add(completed_frame, weight=1)
        
        ttk.Label(completed_frame, text="Completed Downloads", font=('', 9, 'bold')).pack(anchor=tk.W, pady=(0, 5))
        completed_listbox_frame = ttk.Frame(completed_frame)
        completed_listbox_frame.pack(fill=tk.BOTH, expand=True)
        
        self.completed_listbox = tk.Listbox(completed_listbox_frame, height=8)
        completed_scrollbar = ttk.Scrollbar(completed_listbox_frame, orient=tk.VERTICAL, command=self.completed_listbox.yview)
        self.completed_listbox.configure(yscrollcommand=completed_scrollbar.set)
        self.completed_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        completed_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Failed downloads listbox
        failed_frame = ttk.Frame(paned)
        paned.add(failed_frame, weight=1)
        
        failed_header_frame = ttk.Frame(failed_frame)
        failed_header_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(failed_header_frame, text="Failed Downloads", font=('', 9, 'bold')).pack(side=tk.LEFT, anchor=tk.W)
        self.retry_failed_button = ttk.Button(failed_header_frame, text="Retry Failed", 
                                              command=self.retry_failed_downloads, state=tk.DISABLED)
        self.retry_failed_button.pack(side=tk.RIGHT, padx=5)
        
        failed_listbox_frame = ttk.Frame(failed_frame)
        failed_listbox_frame.pack(fill=tk.BOTH, expand=True)
        
        self.failed_listbox = tk.Listbox(failed_listbox_frame, height=8)
        failed_scrollbar = ttk.Scrollbar(failed_listbox_frame, orient=tk.VERTICAL, command=self.failed_listbox.yview)
        self.failed_listbox.configure(yscrollcommand=failed_scrollbar.set)
        self.failed_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        failed_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Log area
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding="10")
        log_frame.grid(row=6, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)
        
        # Log controls frame
        log_controls_frame = ttk.Frame(log_frame)
        log_controls_frame.pack(fill=tk.X, pady=(0, 5))
        
        clear_log_button = ttk.Button(log_controls_frame, text="Clear Log", command=self.clear_log)
        clear_log_button.pack(side=tk.RIGHT, padx=5)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)
        main_frame.rowconfigure(5, weight=1)
        main_frame.rowconfigure(6, weight=1)
        
        # Keyboard shortcuts
        self.root.bind('<Return>', lambda e: self.start_download() if not self.is_downloading else None)
        self.root.bind('<Escape>', lambda e: self.stop_download() if self.is_downloading else None)
        self.root.bind('<Control-c>', self._copy_selected_path_keyboard)
        self.root.bind('<Control-f>', lambda e: search_entry.focus())
        
        # System tray setup (Windows only for now)
        self._setup_system_tray()
        
    def log(self, message):
        """Add message to log"""
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def clear_log(self):
        """Clear the log area"""
        self.log_text.delete(1.0, tk.END)
        
    def browse_directory(self):
        """Browse for local directory"""
        directory = filedialog.askdirectory()
        if directory:
            self.local_dir_entry.delete(0, tk.END)
            self.local_dir_entry.insert(0, directory)
    
    def open_local_folder(self):
        """Open the local download directory in file explorer"""
        local_dir = self.local_dir_entry.get().strip()
        if not local_dir:
            messagebox.showwarning("Warning", "Please specify a local directory first.")
            return
        
        if not os.path.exists(local_dir):
            messagebox.showwarning("Warning", f"Directory does not exist:\n{local_dir}")
            return
        
        # Open folder in file explorer (Windows)
        try:
            os.startfile(local_dir)
        except AttributeError:
            # Linux/Mac fallback
            try:
                subprocess.Popen(['xdg-open', local_dir])
            except:
                try:
                    subprocess.Popen(['open', local_dir])
                except:
                    messagebox.showerror("Error", "Could not open folder. Please open manually.")
    
    def _on_search_change(self, *args):
        """Handle search/filter text change"""
        search_text = self.search_var.get().lower()
        
        # Get all items from our tracking set (includes both attached and detached)
        all_items = list(self.all_tree_items) if hasattr(self, 'all_tree_items') else []
        
        # If we don't have tracking, build it from current treeview
        if not all_items:
            all_items = list(self.tree.get_children(''))
            self.all_tree_items = set(all_items)
        
        # Detach all items first
        for item_id in list(self.tree.get_children('')):
            try:
                self.tree.detach(item_id)
            except:
                pass
        
        # Re-attach items that match search
        if not search_text:
            # Show all files
            for item_id in all_items:
                try:
                    self.tree.reattach(item_id, '', 'end')
                except:
                    pass
        else:
            # Only show matching files
            for item_id in all_items:
                try:
                    file_path = self.tree.item(item_id, 'text')
                    if search_text in file_path.lower():
                        self.tree.reattach(item_id, '', 'end')
                except:
                    pass
    
    def _clear_search(self):
        """Clear the search filter"""
        self.search_var.set("")
        # Re-attach all items
        for item_id in self.tree.get_children():
            self.tree.reattach(item_id, '', 'end')
    
    def _on_treeview_right_click(self, event):
        """Handle right-click on treeview"""
        item = self.tree.selection()[0] if self.tree.selection() else None
        if item:
            # Enable/disable menu items based on context
            file_path = self.tree.item(item, 'text')
            values = self.tree.item(item, 'values')
            status = values[1] if len(values) > 1 else ""
            
            # Enable retry only if failed
            self.tree_context_menu.entryconfig("Retry Download", 
                state=tk.NORMAL if status.startswith("Failed") else tk.DISABLED)
            
            self.tree_context_menu.post(event.x_root, event.y_root)
    
    def _copy_selected_path(self):
        """Copy selected file path to clipboard"""
        selection = self.tree.selection()
        if selection:
            file_path = self.tree.item(selection[0], 'text')
            self.root.clipboard_clear()
            self.root.clipboard_append(file_path)
    
    def _copy_selected_path_keyboard(self, event):
        """Copy selected file path to clipboard (keyboard shortcut)"""
        self._copy_selected_path()
        return "break"  # Prevent default behavior
    
    def _open_selected_file_location(self):
        """Open the location of the selected file"""
        selection = self.tree.selection()
        if not selection:
            return
        
        file_path = self.tree.item(selection[0], 'text')
        local_dir = self.local_dir_entry.get().strip()
        
        # Find the local path for this file
        local_path = None
        for remote_path, local_path_item in self.failed_downloads_dict.items():
            if remote_path == file_path:
                local_path = local_path_item
                break
        
        if not local_path:
            # Try to construct from remote path
            if file_path.startswith('/'):
                rel_path = file_path[1:]
            else:
                rel_path = file_path
            local_path = os.path.join(local_dir, rel_path)
        
        # Open the directory containing the file
        if os.path.exists(local_path):
            folder_path = os.path.dirname(local_path)
            try:
                os.startfile(folder_path)
            except AttributeError:
                try:
                    subprocess.Popen(['xdg-open', folder_path])
                except:
                    try:
                        subprocess.Popen(['open', folder_path])
                    except:
                        messagebox.showerror("Error", "Could not open folder.")
        else:
            messagebox.showwarning("Warning", f"File not found:\n{local_path}")
    
    def _retry_selected_file(self):
        """Retry downloading the selected file"""
        selection = self.tree.selection()
        if not selection:
            return
        
        file_path = self.tree.item(selection[0], 'text')
        
        # Find local path
        local_path = None
        if file_path in self.failed_downloads_dict:
            local_path = self.failed_downloads_dict[file_path]
        else:
            # Construct from remote path
            local_dir = self.local_dir_entry.get().strip()
            if file_path.startswith('/'):
                rel_path = file_path[1:]
            else:
                rel_path = file_path
            local_path = os.path.join(local_dir, rel_path)
        
        if local_path:
            # Re-queue for download
            self.download_queue.put((file_path, local_path))
            # Track queued files
            with self.stats['lock']:
                if 'queued_files' not in self.stats:
                    self.stats['queued_files'] = 0
                self.stats['queued_files'] += 1
            # Remove from failed tracking
            if file_path in self.failed_downloads_dict:
                del self.failed_downloads_dict[file_path]
            # Update status
            if file_path in self.file_to_item:
                item_id = self.file_to_item[file_path]
                current_values = self.tree.item(item_id, 'values')
                if current_values:
                    size = current_values[0] if len(current_values) > 0 else "Unknown"
                    self.tree.item(item_id, values=(size, "Pending", ""))
            self.log(f"Re-queued file for retry: {file_path}")
    
    def _setup_system_tray(self):
        """Setup system tray icon (Windows only)"""
        try:
            import pystray
            from PIL import Image
            
            # Try to load the actual icon file
            icon_path = os.path.join(os.path.dirname(__file__), "donload.png")
            if os.path.exists(icon_path):
                try:
                    image = Image.open(icon_path)
                    # Resize to appropriate size for tray icon (usually 16x16 or 32x32)
                    image = image.resize((64, 64), Image.Resampling.LANCZOS)
                except Exception:
                    # Fallback to simple icon if loading fails
                    image = Image.new('RGB', (64, 64), color='white')
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(image)
                    draw.ellipse([16, 16, 48, 48], fill='blue')
            else:
                # Fallback to simple icon if file doesn't exist
                image = Image.new('RGB', (64, 64), color='white')
                from PIL import ImageDraw
                draw = ImageDraw.Draw(image)
                draw.ellipse([16, 16, 48, 48], fill='blue')
            
            # Create menu
            menu = pystray.Menu(
                pystray.MenuItem('Show', self._show_window),
                pystray.MenuItem('Hide', self._hide_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit', self._quit_app)
            )
            
            self.tray_icon = pystray.Icon("ftp donloader", image, "ftp donloader", menu)
            
            # Start tray icon in a separate thread
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
            
            # Handle window close to minimize to tray
            self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
            
        except ImportError:
            # pystray not available, skip tray icon
            self.tray_icon = None
        except Exception as e:
            # Any other error, skip tray icon
            self.tray_icon = None
    
    def _show_window(self, icon=None, item=None):
        """Show the main window"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
    
    def _hide_window(self, icon=None, item=None):
        """Hide the main window"""
        self.root.withdraw()
    
    def _on_closing(self):
        """Handle window closing - minimize to tray if available"""
        if self.tray_icon:
            self._hide_window()
        else:
            self._quit_app()
    
    def _quit_app(self, icon=None, item=None):
        """Quit the application"""
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()
    
    def _show_tray_notification(self, title, message, duration=5):
        """Show a system tray notification (Windows balloon)"""
        try:
            # Windows-specific notification
            if sys.platform == 'win32':
                try:
                    import win10toast
                    toaster = win10toast.ToastNotifier()
                    toaster.show_toast(title, message, duration=duration, threaded=True)
                except ImportError:
                    # Fallback to pystray notification if available
                    if self.tray_icon:
                        self.tray_icon.notify(message, title)
        except:
            pass
    
    def test_connection(self):
        """Test FTP connection and try to get server statistics"""
        self.test_connection_button.config(state=tk.DISABLED)
        self.log("Testing FTP connection...")
        
        def test_thread():
            try:
                host = self.host_entry.get().strip()
                port = int(self.port_entry.get() or 21)
                username = self.username_entry.get().strip()
                password = self.password_entry.get()
                use_tls = self.use_tls_var.get()
                
                # Connect to FTP server
                if use_tls:
                    ftp = ftplib.FTP_TLS()
                    ftp.connect(host, port)
                    ftp.login(username, password)
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP()
                    ftp.connect(host, port)
                    if username or password:
                        ftp.login(username, password)
                
                # Try to get server statistics (some servers support this)
                stats_info = []
                try:
                    # Try STAT command (some servers provide stats)
                    response = ftp.sendcmd('STAT')
                    if response and len(response) > 0:
                        stats_info.append(f"Server Status: {response[:200]}")
                except:
                    pass
                
                try:
                    # Try SITE STAT (some servers support this)
                    response = ftp.sendcmd('SITE STAT')
                    if response and len(response) > 0:
                        stats_info.append(f"SITE STAT: {response[:200]}")
                except:
                    pass
                
                try:
                    # Try SYST to get server type
                    syst = ftp.sendcmd('SYST')
                    if syst:
                        stats_info.append(f"Server Type: {syst}")
                        # Check if it's PureFTPd
                        if 'pure-ftpd' in syst.lower() or 'pureftpd' in syst.lower():
                            stats_info.append("Detected PureFTPd - using optimized MLSD listing")
                except:
                    pass
                
                # Try PureFTPd specific features
                try:
                    # PureFTPd might support FEAT to see available features
                    features = ftp.sendcmd('FEAT')
                    if features and ('MLSD' in features or 'MLST' in features):
                        stats_info.append("Server supports MLSD (Machine Listing) - optimal for directory scanning")
                except:
                    pass
                
                ftp.quit()
                
                success_msg = f"✓ Connected to {host} successfully!"
                if stats_info:
                    success_msg += "\n\nServer Information:\n" + "\n".join(stats_info)
                else:
                    success_msg += "\n\n(Server statistics not available - will count files during scan)"
                
                self.root.after(0, lambda: self.log(success_msg))
                self.root.after(0, lambda: self.test_connection_button.config(state=tk.NORMAL))
                self.root.after(0, lambda msg=success_msg: messagebox.showinfo("Success", msg))
                
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self.log(f"✗ Connection failed: {msg}"))
                self.root.after(0, lambda: self.test_connection_button.config(state=tk.NORMAL))
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Error", f"Connection failed: {msg}"))
        
        threading.Thread(target=test_thread, daemon=True).start()
    
    def scan_ftp_server(self):
        """Scan FTP server using ftplib to build complete file list with 1:1 structure"""
        self.test_connection_button.config(state=tk.DISABLED)
        self.log("Scanning FTP server to build complete file list (1:1 structure)...")
        self.tree.delete(*self.tree.get_children())
        self.file_list = []
        
        def scan_thread():
            try:
                host = self.host_entry.get().strip()
                port = int(self.port_entry.get() or 21)
                username = self.username_entry.get().strip()
                password = self.password_entry.get()
                remote_path = self.remote_path_entry.get().strip() or "/"
                use_tls = self.use_tls_var.get()
                
                # Connect to FTP server
                if use_tls:
                    ftp = ftplib.FTP_TLS()
                    ftp.connect(host, port)
                    ftp.login(username, password)
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP()
                    ftp.connect(host, port)
                    if username or password:
                        ftp.login(username, password)
                
                self.root.after(0, lambda: self.log(f"Connected to {host}, scanning entire server structure..."))
                
                # Recursively scan directory - this will list EVERYTHING
                self._scan_directory_ftp(ftp, remote_path, remote_path)
                
                ftp.quit()
                
                # Update UI
                file_count = len(self.file_list)
                self.root.after(0, self._update_file_list)
                self.root.after(0, lambda: self.test_connection_button.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.log(f"Scan complete! Found {file_count} files with exact server structure."))
                self.root.after(0, lambda: self.download_button.config(state=tk.NORMAL))
                
            except Exception as e:
                import traceback
                error_msg = str(e)
                traceback_str = traceback.format_exc()
                self.root.after(0, lambda: self.log(f"Scan error: {error_msg}"))
                self.root.after(0, lambda: self.log(f"Traceback: {traceback_str}"))
                self.root.after(0, lambda: self.test_connection_button.config(state=tk.NORMAL))
                self.root.after(0, lambda: messagebox.showerror("Error", f"Scan failed: {error_msg}"))
        
        threading.Thread(target=scan_thread, daemon=True).start()
    
    def _scan_directory_ftp(self, ftp, current_path, base_path):
        """Recursively scan FTP directory"""
        try:
            if current_path != '/':
                try:
                    ftp.cwd(current_path)
                except Exception as e:
                    self.root.after(0, lambda: self.log(f"Warning: Could not access {current_path}: {str(e)}"))
                    return
            
            items = []
            try:
                # Try MLSD first (more reliable)
                for item in ftp.mlsd():
                    items.append(item)
            except Exception as e1:
                # Fallback to LIST
                try:
                    lines = []
                    ftp.retrlines('LIST', lines.append)
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 9:
                            name = ' '.join(parts[8:])
                            is_dir = parts[0].startswith('d')
                            size = parts[4] if len(parts) > 4 else 'Unknown'
                            items.append((name, {'type': 'dir' if is_dir else 'file', 'size': size}))
                except Exception as e2:
                    self.root.after(0, lambda: self.log(f"Warning: Could not list {current_path}: {str(e2)}"))
                    return
            
            for name, info in items:
                if name in ['.', '..']:
                    continue
                
                remote_path = os.path.join(current_path, name).replace('\\', '/')
                
                if info.get('type') == 'dir':
                    # Recursively scan subdirectory
                    self._scan_directory_ftp(ftp, remote_path, base_path)
                else:
                    # Add file to list
                    size = info.get('size', 'Unknown')
                    self.file_list.append((remote_path, size))
                    # Update stats
                    with self.stats['lock']:
                        self.stats['total'] += 1
                        # Add file size to total size (parse size - could be string or int)
                        if isinstance(size, (int, float)):
                            size_bytes = int(size)
                        else:
                            size_bytes = self._parse_size(size)
                        self.stats['total_size'] += size_bytes
                    # Update UI periodically
                    if len(self.file_list) % 100 == 0:
                        count = len(self.file_list)
                        self.root.after(0, lambda c=count: self.log(f"Found {c} files so far..."))
        except Exception as e:
            self.root.after(0, lambda: self.log(f"Error scanning {current_path}: {str(e)}"))
    
    def _try_recursive_list(self, ftp, remote_base, local_dir):
        """Try to use PureFTPd's recursive LIST -R command for faster scanning
        
        Returns True if successful and all files were discovered, False otherwise.
        """
        try:
            self.root.after(0, lambda: self.log("Attempting recursive LIST -R (PureFTPd feature)..."))
            
            # Change to base directory
            if remote_base != '/':
                ftp.cwd(remote_base)
            else:
                ftp.cwd('/')
            
            # Try LIST -R for recursive listing (PureFTPd feature)
            # PureFTPd supports LIST -R for recursive directory listing
            # Also try with -a to include hidden files/directories
            lines = []
            try:
                # Try with -R flag for recursive listing and -a for all files (including hidden)
                # This should get everything in one command
                ftp.retrlines('LIST -R -a', lines.append)
                self.root.after(0, lambda: self.log("Using LIST -R -a (recursive with hidden files)"))
            except:
                try:
                    # Try just -R without -a (still recursive, but might miss hidden files)
                    ftp.retrlines('LIST -R', lines.append)
                    self.root.after(0, lambda: self.log("Using LIST -R (recursive listing)"))
                except:
                    try:
                        # If that fails, try without the space (some servers might need it differently)
                        ftp.retrlines('LIST-R', lines.append)
                        self.root.after(0, lambda: self.log("Using LIST-R (alternative format)"))
                    except Exception as e:
                        self.root.after(0, lambda err=str(e): self.log(f"Recursive LIST not supported: {err}"))
                        return False
            
            if not lines:
                return False
            
            self.root.after(0, lambda: self.log(f"Recursive LIST successful! Parsing {len(lines)} lines..."))
            
            # Parse the recursive listing
            # PureFTPd recursive LIST format shows directory paths followed by their contents
            current_dir = remote_base.rstrip('/') or '/'
            files_found = 0
            dirs_found = set()
            
            # Debug: log first few lines to understand the format
            if len(lines) > 0:
                sample_lines = lines[:10] if len(lines) >= 10 else lines
                self.root.after(0, lambda sample=sample_lines: self.log(f"Sample of first {len(sample)} lines from recursive listing (for debugging):"))
                for i, sample_line in enumerate(sample_lines[:5]):  # Show first 5
                    self.root.after(0, lambda sl=sample_line, idx=i: self.log(f"  [{idx}]: {sl[:100]}"))  # First 100 chars
            
            for line in lines:
                # Skip empty lines
                if not line.strip():
                    continue
                
                # Check if this is a directory path indicator
                # PureFTPd format can be:
                # - "/path/to/dir:"
                # - "path/to/dir:"
                # - "./path/to/dir:"
                # - Just a path without colon in some cases
                # Directory headers typically end with ':' and don't start with permissions
                line_stripped = line.strip()
                
                # Check if this looks like a directory path header (ends with ':' and doesn't look like a file entry)
                if line_stripped.endswith(':'):
                    potential_dir = line_stripped[:-1].strip()  # Remove trailing ':'
                    # Skip if it looks like a file entry (starts with permissions like '-rw-' or 'drwx')
                    # Directory headers don't start with file permissions
                    if potential_dir and not (line_stripped.startswith('-') or 
                                             line_stripped.startswith('d') or 
                                             line_stripped.startswith('l') or
                                             line_stripped.startswith('c') or
                                             line_stripped.startswith('b') or
                                             line_stripped.startswith('p') or
                                             line_stripped.startswith('s') or
                                             len(line_stripped.split()) > 1):  # File entries have multiple space-separated fields
                        # This is likely a directory path header
                        if potential_dir.startswith('/'):
                            current_dir = potential_dir
                        elif potential_dir.startswith('.'):
                            # Handle relative paths starting with .
                            if potential_dir == '.':
                                current_dir = remote_base.rstrip('/') or '/'
                            else:
                                # Remove leading ./
                                clean_path = potential_dir.lstrip('./')
                                if remote_base == '/':
                                    current_dir = f"/{clean_path}" if clean_path else '/'
                                else:
                                    current_dir = f"{remote_base.rstrip('/')}/{clean_path}".replace('//', '/') if clean_path else remote_base
                        else:
                            # Relative path, make it absolute
                            if remote_base == '/':
                                current_dir = f"/{potential_dir}" if potential_dir else '/'
                            else:
                                current_dir = f"{remote_base.rstrip('/')}/{potential_dir}".replace('//', '/') if potential_dir else remote_base
                        
                        # Normalize the path
                        current_dir = current_dir.replace('\\', '/').rstrip('/') or '/'
                        dirs_found.add(current_dir)
                        continue
                
                # Also check for directory headers without colon (some formats)
                # If line doesn't start with permissions and doesn't have spaces, might be a path
                if ':' not in line and not line_stripped.startswith(('-', 'd', 'l', 'c', 'b', 'p', 's')) and ' ' not in line_stripped:
                    # Might be a directory path without colon - but be careful not to misidentify
                    # Only treat as directory if it looks like a path (contains /)
                    if '/' in line_stripped or line_stripped == '.' or line_stripped == '..':
                        potential_dir = line_stripped
                        if potential_dir.startswith('/'):
                            current_dir = potential_dir
                        elif potential_dir.startswith('.'):
                            if potential_dir == '.':
                                current_dir = remote_base.rstrip('/') or '/'
                            else:
                                clean_path = potential_dir.lstrip('./')
                                if remote_base == '/':
                                    current_dir = f"/{clean_path}" if clean_path else '/'
                                else:
                                    current_dir = f"{remote_base.rstrip('/')}/{clean_path}".replace('//', '/') if clean_path else remote_base
                        else:
                            if remote_base == '/':
                                current_dir = f"/{potential_dir}" if potential_dir else '/'
                            else:
                                current_dir = f"{remote_base.rstrip('/')}/{potential_dir}".replace('//', '/') if potential_dir else remote_base
                        current_dir = current_dir.replace('\\', '/').rstrip('/') or '/'
                        dirs_found.add(current_dir)
                        continue
                
                # Parse file/directory entry (starts with permissions like "drwxr-xr-x" or "-rw-r--r--")
                # Try to parse as a file/directory entry
                parts = line.split()
                
                # Check if this looks like a file entry (has permissions)
                # File entries typically have at least 9 parts: permissions, links, owner, group, size, date, time, name
                # But some formats might have fewer parts, so be more lenient
                if len(parts) >= 5 and (parts[0].startswith('d') or parts[0].startswith('-') or 
                                       parts[0].startswith('l') or parts[0].startswith('c') or 
                                       parts[0].startswith('b') or parts[0].startswith('p') or 
                                       parts[0].startswith('s')):
                    # Extract name - it's usually the last part(s), but handle spaces in filenames
                    # Try to find where the filename starts (usually after date/time)
                    # Common format: permissions links owner group size month day time name
                    # Or: permissions links owner group size date time name
                    if len(parts) >= 9:
                        # Standard format - name starts at index 8
                        name = ' '.join(parts[8:])
                    elif len(parts) >= 6:
                        # Shorter format - name might be at the end
                        # Try to detect: if parts[4] looks like a number (size), then parts[5+] is name
                        try:
                            int(parts[4])  # If this works, parts[4] is size
                            name = ' '.join(parts[5:])
                        except (ValueError, IndexError):
                            # parts[4] is not a number, might be part of name
                            name = ' '.join(parts[4:])
                    else:
                        # Very short format, just use last part as name
                        name = parts[-1] if parts else ''
                    
                    if name in ['.', '..']:
                        continue
                    
                    is_dir = parts[0].startswith('d')
                    # Try to get size
                    try:
                        if len(parts) >= 5:
                            size = parts[4]
                            # Try to convert to int to verify it's a size
                            int(size)
                        else:
                            size = 'Unknown'
                    except (ValueError, IndexError):
                        size = 'Unknown'
                    
                    # Build full path
                    if current_dir == '/':
                        remote_path = f"/{name}"
                    else:
                        remote_path = f"{current_dir.rstrip('/')}/{name}"
                    remote_path = remote_path.replace('\\', '/')
                    
                    if is_dir:
                        # Add to scanned dirs to prevent re-scanning
                        dirs_found.add(remote_path)
                        with self.scanned_dirs_lock:
                            self.scanned_dirs.add(remote_path)
                    else:
                        # It's a file - check if already processed before queueing
                        with self.stats['lock']:
                            # Check if already downloaded
                            if 'downloaded_paths' in self.stats and remote_path in self.stats['downloaded_paths']:
                                continue  # Skip already downloaded files
                            
                            # Check if currently downloading
                            if 'downloading_paths' in self.stats and remote_path in self.stats['downloading_paths']:
                                continue  # Skip files currently being downloaded
                        
                        # Check if already in file_list (already queued)
                        if any(fp == remote_path for fp, _ in self.file_list):
                            continue  # Skip files already in the list
                        
                        # Calculate local path
                        if remote_path.startswith('/'):
                            rel_path = remote_path[1:]
                        else:
                            rel_path = remote_path
                        
                        local_path = os.path.join(local_dir, rel_path)
                        
                        # Check if file already exists locally
                        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                            # File already exists, mark as downloaded
                            with self.stats['lock']:
                                if 'downloaded_paths' not in self.stats:
                                    self.stats['downloaded_paths'] = set()
                                self.stats['downloaded_paths'].add(remote_path)
                            continue  # Skip already existing files
                        
                        # Queue it
                        files_found += 1
                        self.download_queue.put((remote_path, local_path))
                        # Track queued files
                        with self.stats['lock']:
                            if 'queued_files' not in self.stats:
                                self.stats['queued_files'] = 0
                            self.stats['queued_files'] += 1
                        
                        # Update file list
                        self.file_list.append((remote_path, size))
                        
                        # Update stats
                        with self.stats['lock']:
                            self.stats['total'] += 1
                            # Add file size to total size (parse size - could be string or int)
                            if isinstance(size, (int, float)):
                                size_bytes = int(size)
                            else:
                                size_bytes = self._parse_size(size)
                            self.stats['total_size'] += size_bytes
                        
                        # Batch UI updates
                        if len(self.file_list) % 20 == 0:
                            batch_files = self.file_list[-20:]
                            self.root.after(0, lambda batch=batch_files: self._batch_add_files_to_treeview(batch))
                        
                        # Log progress
                        if files_found % 200 == 0:
                            self.root.after(0, lambda f=files_found: self.log(f"Discovered {f} files from recursive listing..."))
            
            # Add remaining files to treeview
            if len(self.file_list) > 0:
                remaining = self.file_list[len(self.file_list) - (len(self.file_list) % 20):]
                if remaining:
                    self.root.after(0, lambda batch=remaining: self._batch_add_files_to_treeview(batch))
            
            # Verify we got substantial results (sanity check)
            if files_found == 0 and len(dirs_found) <= 1:
                # Might not have gotten everything, fall back to standard scanning
                self.root.after(0, lambda: self.log("Warning: Recursive listing returned no files, falling back to standard scanning."))
                return False
            
            # Log final count - use files_found which is accurate for this recursive listing
            self.root.after(0, lambda f=files_found, d=len(dirs_found): self.log(f"Recursive listing complete! Found {f} files in {d} directories."))
            self.root.after(0, lambda: self.log("Recursive listing succeeded - standard scanners will verify completeness and scan any missed directories."))
            
            # IMPORTANT: Don't mark directories as scanned - let standard scanners verify them
            # The recursive listing parser is unreliable and misses many files, so we must let
            # the standard scanners process all directories to ensure completeness
            # The recursive listing helps by quickly queueing some files, but standard scanners
            # will catch any directories/files that were missed due to parsing issues
            
            # DO NOT mark directories as scanned - this would prevent standard scanners from processing them
            # with self.scanned_dirs_lock:
            #     for dir_path in dirs_found:
            #         self.scanned_dirs.add(dir_path)
            
            return True  # Return True to indicate recursive listing worked, but scanners will still verify
            
        except Exception as e:
            self.root.after(0, lambda: self.log(f"Recursive LIST failed, falling back to standard scanning: {str(e)}"))
            return False
    
    def _scan_and_queue_files_ftputil(self, ftp_host, current_path, base_path, local_dir, dir_queue=None):
        """Recursively scan FTP directory using ftputil and queue files for download
        
        If dir_queue is provided, directories are added to the queue for parallel processing.
        Otherwise, directories are processed recursively in this thread.
        """
        try:
            # Check if this directory has already been scanned (for parallel scanners)
            if dir_queue is not None:
                with self.scanned_dirs_lock:
                    if current_path in self.scanned_dirs:
                        return  # Already scanned by another scanner
                    self.scanned_dirs.add(current_path)
            
            # Normalize path for ftputil (it works with absolute paths)
            if not current_path.startswith('/'):
                current_path = '/' + current_path
            
            # Change to current directory
            try:
                ftp_host.chdir(current_path)
            except Exception:
                return  # Can't access this directory
            
            # Use ftputil's listdir to get directory contents
            try:
                items = ftp_host.listdir(ftp_host.curdir)
            except Exception:
                return  # Can't list directory
            
            # Separate files and directories
            dirs = []
            files = []
            
            for name in items:
                if name in ['.', '..']:
                    continue
                
                # Build full path
                if current_path == '/':
                    remote_path = f"/{name}"
                else:
                    remote_path = f"{current_path.rstrip('/')}/{name}"
                remote_path = remote_path.replace('\\', '/')
                
                # Use ftputil's isfile/isdir to check type
                # After chdir, we can use relative paths (just the name)
                try:
                    if ftp_host.path.isdir(name):
                        dirs.append(remote_path)
                    elif ftp_host.path.isfile(name):
                        # Get file size
                        try:
                            size = ftp_host.path.getsize(name)
                            files.append((remote_path, {'type': 'file', 'size': size}))
                        except Exception:
                            files.append((remote_path, {'type': 'file', 'size': 'Unknown'}))
                except Exception:
                    # If we can't determine type, skip it
                    continue
            
            # Queue files first
            for remote_path, info in files:
                # Check if file is already downloaded, downloading, or queued
                with self.stats['lock']:
                    # Check if already downloaded
                    if 'downloaded_paths' in self.stats and remote_path in self.stats['downloaded_paths']:
                        continue  # Skip already downloaded files
                    
                    # Check if currently downloading
                    if 'downloading_paths' in self.stats and remote_path in self.stats['downloading_paths']:
                        continue  # Skip files currently being downloaded
                
                # Check if already in file_list (already queued)
                if any(fp == remote_path for fp, _ in self.file_list):
                    continue  # Skip files already in the list
                
                # Calculate local path - preserve exact 1:1 structure
                if remote_path.startswith('/'):
                    rel_path = remote_path[1:]
                else:
                    rel_path = remote_path
                
                local_path = os.path.join(local_dir, rel_path)
                
                # Check if file already exists locally
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    # File already exists, mark as downloaded
                    with self.stats['lock']:
                        if 'downloaded_paths' not in self.stats:
                            self.stats['downloaded_paths'] = set()
                        self.stats['downloaded_paths'].add(remote_path)
                    continue  # Skip already existing files
                
                # Add to queue
                self.download_queue.put((remote_path, local_path))
                # Track queued files
                with self.stats['lock']:
                    if 'queued_files' not in self.stats:
                        self.stats['queued_files'] = 0
                    self.stats['queued_files'] += 1
                
                # Update file list for UI
                size = info.get('size', 'Unknown')
                # Get raw size in bytes for total_size calculation
                size_bytes = self._parse_size(size) if not isinstance(size, (int, float)) else (int(size) if isinstance(size, (int, float)) else 0)
                if isinstance(size, (int, float)):
                    # Format size for display
                    if size >= 1024 * 1024 * 1024:
                        size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"
                    elif size >= 1024 * 1024:
                        size_str = f"{size / (1024 * 1024):.2f} MB"
                    elif size >= 1024:
                        size_str = f"{size / 1024:.2f} KB"
                    else:
                        size_str = f"{size} B"
                else:
                    size_str = str(size)
                
                self.file_list.append((remote_path, size_str))
                
                # Increment total count when file is discovered (not when processed)
                with self.stats['lock']:
                    self.stats['total'] += 1
                    # Add file size to total size (use raw bytes, not formatted string)
                    self.stats['total_size'] += size_bytes
                
                # Batch UI updates for better performance (update every 20 files for less overhead)
                if len(self.file_list) % 20 == 0:
                    # Batch update UI
                    batch_files = self.file_list[-20:]
                    self.root.after(0, lambda batch=batch_files: self._batch_add_files_to_treeview(batch))
                
                # Log progress periodically (less frequent to reduce overhead)
                if len(self.file_list) % 200 == 0:
                    count = len(self.file_list)
                    with self.stats['lock']:
                        total_count = self.stats['total']
                    self.root.after(0, lambda c=count, t=total_count: self.log(f"Discovered {c} files, queued for download... (Total: {t})"))
            
            # Then recursively scan directories
            if dir_queue is not None:
                # Parallel scanning - add directories to queue
                for remote_path in dirs:
                    dir_queue.put(remote_path)
            else:
                # Sequential scanning - process directories recursively
                for remote_path in dirs:
                    self._scan_and_queue_files_ftputil(ftp_host, remote_path, base_path, local_dir, dir_queue)
                
        except Exception as e:
            pass  # Silently continue on errors
    
    def _scan_and_queue_files(self, ftp, current_path, base_path, local_dir, dir_queue=None):
        """Recursively scan FTP directory and queue files for download
        
        If dir_queue is provided, directories are added to the queue for parallel processing.
        Otherwise, directories are processed recursively in this thread.
        """
        try:
            # Check if this directory has already been scanned (for parallel scanners)
            if dir_queue is not None:
                with self.scanned_dirs_lock:
                    if current_path in self.scanned_dirs:
                        return  # Already scanned by another scanner
                    self.scanned_dirs.add(current_path)
            
            # Change to current directory
            if current_path != '/':
                try:
                    ftp.cwd(current_path)
                except Exception as e:
                    return
            else:
                ftp.cwd('/')
            
            items = []
            try:
                # Try MLSD first (best for PureFTPd and modern servers - structured, reliable, has type/size)
                # MLSD is more efficient than NLST+type checking for servers that support it
                for item in ftp.mlsd(facts=['type', 'size']):
                    items.append(item)
            except:
                # Fallback to NLST (fastest - just filenames, but requires type checking)
                try:
                    names = ftp.nlst()
                    # Process in batch to reduce round trips
                    for name in names:
                        if name in ['.', '..']:
                            continue
                        # For speed, we'll determine type and size lazily
                        # Just mark as unknown for now, we'll check type when needed
                        items.append((name, {'type': 'unknown', 'size': 'Unknown'}))
                except:
                    # Final fallback to LIST (slowest, but most compatible)
                    try:
                        lines = []
                        ftp.retrlines('LIST', lines.append)
                        for line in lines:
                            parts = line.split()
                            if len(parts) >= 9:
                                name = ' '.join(parts[8:])
                                is_dir = parts[0].startswith('d')
                                size = parts[4] if len(parts) > 4 else 'Unknown'
                                items.append((name, {'type': 'dir' if is_dir else 'file', 'size': size}))
                    except:
                        return
            
            # Separate files and directories
            dirs = []
            files = []
            
            for name, info in items:
                if name in ['.', '..']:
                    continue
                
                # Build path preserving exact structure
                if current_path == '/':
                    remote_path = f"/{name}"
                else:
                    remote_path = f"{current_path.rstrip('/')}/{name}"
                remote_path = remote_path.replace('\\', '/')
                
                # If type is unknown (from NLST), check it quickly
                item_type = info.get('type', 'unknown')
                if item_type == 'unknown':
                    # Quick check: try to CWD into it (fastest way to check if dir)
                    try:
                        current_dir = ftp.pwd()
                        ftp.cwd(name)
                        ftp.cwd(current_dir)
                        item_type = 'dir'
                    except:
                        item_type = 'file'
                
                if item_type == 'dir' or item_type == 'cdir' or item_type == 'pdir':
                    dirs.append(remote_path)
                else:
                    files.append((remote_path, info))
            
            # Queue files first
            for remote_path, info in files:
                # Check if file is already downloaded, downloading, or queued
                with self.stats['lock']:
                    # Check if already downloaded
                    if 'downloaded_paths' in self.stats and remote_path in self.stats['downloaded_paths']:
                        continue  # Skip already downloaded files
                    
                    # Check if currently downloading
                    if 'downloading_paths' in self.stats and remote_path in self.stats['downloading_paths']:
                        continue  # Skip files currently being downloaded
                
                # Check if already in file_list (already queued)
                if any(fp == remote_path for fp, _ in self.file_list):
                    continue  # Skip files already in the list
                
                # Calculate local path - preserve exact 1:1 structure
                if remote_path.startswith('/'):
                    rel_path = remote_path[1:]
                else:
                    rel_path = remote_path
                
                local_path = os.path.join(local_dir, rel_path)
                
                # Check if file already exists locally
                if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                    # File already exists, mark as downloaded
                    with self.stats['lock']:
                        if 'downloaded_paths' not in self.stats:
                            self.stats['downloaded_paths'] = set()
                        self.stats['downloaded_paths'].add(remote_path)
                    continue  # Skip already existing files
                
                # Add to queue
                self.download_queue.put((remote_path, local_path))
                # Track queued files
                with self.stats['lock']:
                    if 'queued_files' not in self.stats:
                        self.stats['queued_files'] = 0
                    self.stats['queued_files'] += 1
                
                # Update file list for UI
                size = info.get('size', 'Unknown')
                # Get raw size in bytes for total_size calculation
                if isinstance(size, (int, float)):
                    size_bytes = int(size)
                else:
                    size_bytes = self._parse_size(size)
                
                self.file_list.append((remote_path, size))
                
                # Increment total count when file is discovered (not when processed)
                with self.stats['lock']:
                    self.stats['total'] += 1
                    # Add file size to total size (use raw bytes)
                    self.stats['total_size'] += size_bytes
                
                # Batch UI updates for better performance (update every 20 files for less overhead)
                if len(self.file_list) % 20 == 0:
                    # Batch update UI
                    batch_files = self.file_list[-20:]
                    self.root.after(0, lambda batch=batch_files: self._batch_add_files_to_treeview(batch))
                
                # Log progress periodically (less frequent to reduce overhead)
                if len(self.file_list) % 200 == 0:
                    count = len(self.file_list)
                    with self.stats['lock']:
                        total_count = self.stats['total']
                    self.root.after(0, lambda c=count, t=total_count: self.log(f"Discovered {c} files, queued for download... (Total: {t})"))
            
            # Then recursively scan directories
            for remote_path in dirs:
                self._scan_and_queue_files(ftp, remote_path, base_path, local_dir)
                
        except Exception as e:
            pass  # Silently continue on errors
    
    def _add_file_to_treeview(self, remote_path, size):
        """Add a single file to the treeview immediately"""
        if remote_path not in self.file_to_item:
            item_id = self.tree.insert("", tk.END, text=remote_path, values=(size, "Pending", ""))
            self.file_to_item[remote_path] = item_id
            # Track for search filtering
            if hasattr(self, 'all_tree_items'):
                self.all_tree_items.add(item_id)
    
    def _batch_add_files_to_treeview(self, file_batch):
        """Add multiple files to treeview in a batch for better performance"""
        for remote_path, size in file_batch:
            if remote_path not in self.file_to_item:
                item_id = self.tree.insert("", tk.END, text=remote_path, values=(size, "Pending", ""))
                self.file_to_item[remote_path] = item_id
                # Track for search filtering
                if hasattr(self, 'all_tree_items'):
                    self.all_tree_items.add(item_id)
    
    def _update_file_list(self):
        """Update file list display (rebuilds entire list - used for initial scan)"""
        self.tree.delete(*self.tree.get_children())
        self.file_to_item = {}
        self.downloading_items_moved.clear()  # Reset tracking when rebuilding list
        for remote_path, size in self.file_list:
            item_id = self.tree.insert("", tk.END, text=remote_path, values=(size, "Pending", ""))
            self.file_to_item[remote_path] = item_id
    
    def update_file_status(self, remote_path, status, speed=None):
        """Update the status of a file in the tree view and add to appropriate listbox"""
        if remote_path in self.file_to_item:
            item_id = self.file_to_item[remote_path]
            current_values = self.tree.item(item_id, 'values')
            if current_values:
                # Update status and speed
                if len(current_values) >= 2:
                    size = current_values[0]
                    if speed:
                        self.tree.item(item_id, values=(size, status, speed))
                    else:
                        # Keep existing speed or set to empty
                        existing_speed = current_values[2] if len(current_values) > 2 else ""
                        self.tree.item(item_id, values=(size, status, existing_speed))
                else:
                    self.tree.item(item_id, values=("", status, speed or ""))
            else:
                self.tree.item(item_id, values=("", status, speed or ""))
            
            # Update tags for status images
            if self.has_pil:
                if status == "Completed":
                    self.tree.item(item_id, tags=('success',))
                elif status.startswith("Failed"):
                    self.tree.item(item_id, tags=('failed',))
                else:
                    self.tree.item(item_id, tags=())
            
            # Move downloading files to the top
            if "Downloading" in status:
                if remote_path not in self.downloading_items_moved:
                    # Move to top (index 0)
                    try:
                        children = self.tree.get_children()
                        if children and item_id in children:
                            # Get current index
                            current_index = children.index(item_id)
                            if current_index > 0:
                                # Move to top
                                self.tree.move(item_id, "", 0)
                                self.downloading_items_moved.add(remote_path)
                    except:
                        pass  # Ignore errors if item doesn't exist or can't be moved
            
            # Remove from treeview if completed or failed (keep it in the dedicated listboxes)
            if status == "Completed" or status.startswith("Failed"):
                # Remove from downloading items tracking
                self.downloading_items_moved.discard(remote_path)
                # Remove from treeview after a short delay to allow status update to be visible
                self.root.after(100, lambda rp=remote_path: self._remove_from_treeview(rp))
        
        # Add to completed or failed listbox
        if status == "Completed":
            # Remove from failed downloads if it was retried
            if remote_path in self.failed_downloads:
                self.failed_downloads.remove(remote_path)
            if remote_path in self.failed_downloads_dict:
                del self.failed_downloads_dict[remote_path]
                # Remove from failed listbox
                for i in range(self.failed_listbox.size() - 1, -1, -1):
                    if self.failed_listbox.get(i).startswith(remote_path):
                        self.failed_listbox.delete(i)
                        break
                # Update retry button state
                if not self.failed_downloads_dict:
                    self.retry_failed_button.config(state=tk.DISABLED)
            
            if remote_path not in self.completed_downloads:
                self.completed_downloads.append(remote_path)
                self.completed_listbox.insert(tk.END, remote_path)
                # Auto-scroll to bottom
                self.completed_listbox.see(tk.END)
        elif status.startswith("Failed"):
            if remote_path not in self.failed_downloads:
                self.failed_downloads.append(remote_path)
                # Calculate local path for retry
                local_dir = self.local_dir_entry.get().strip()
                if remote_path.startswith('/'):
                    rel_path = remote_path[1:]
                else:
                    rel_path = remote_path
                local_path = os.path.join(local_dir, rel_path)
                self.failed_downloads_dict[remote_path] = local_path
                
                error_msg = status.replace("Failed: ", "")
                display_text = f"{remote_path} - {error_msg}"
                self.failed_listbox.insert(tk.END, display_text)
                # Auto-scroll to bottom
                self.failed_listbox.see(tk.END)
                
                # Enable retry button if there are failed downloads
                self.retry_failed_button.config(state=tk.NORMAL)
                
                # Auto-retry if enabled
                if self.retry_failed_var.get() and self.is_downloading:
                    # Queue the failed file for retry
                    self.download_queue.put((remote_path, local_path))
                    # Track queued files
                    with self.stats['lock']:
                        if 'queued_files' not in self.stats:
                            self.stats['queued_files'] = 0
                        self.stats['queued_files'] += 1
                    # Remove from failed list temporarily (will be re-added if it fails again)
                    if remote_path in self.failed_downloads:
                        self.failed_downloads.remove(remote_path)
                    if remote_path in self.failed_downloads_dict:
                        del self.failed_downloads_dict[remote_path]
                    # Remove from listbox
                    for i in range(self.failed_listbox.size() - 1, -1, -1):  # Iterate backwards
                        if self.failed_listbox.get(i).startswith(remote_path):
                            self.failed_listbox.delete(i)
                            break
                    # Re-add to treeview as pending
                    if remote_path not in self.file_to_item:
                        size = "Unknown"
                        # Try to find size from file_list
                        for fp, sz in self.file_list:
                            if fp == remote_path:
                                size = sz
                                break
                        item_id = self.tree.insert("", tk.END, text=remote_path, values=(size, "Pending", ""))
                        self.file_to_item[remote_path] = item_id
                    else:
                        # Update status to Pending
                        item_id = self.file_to_item[remote_path]
                        current_values = self.tree.item(item_id, 'values')
                        if current_values:
                            size = current_values[0] if len(current_values) > 0 else "Unknown"
                            self.tree.item(item_id, values=(size, "Pending", ""))
                        # Remove failed tag
                        self.tree.item(item_id, tags=())
                    # Remove from downloaded_paths so it can be retried
                    with self.stats['lock']:
                        if 'downloaded_paths' in self.stats:
                            self.stats['downloaded_paths'].discard(remote_path)
                    self.log(f"Auto-retrying failed download: {remote_path}")
    
    def retry_failed_downloads(self):
        """Retry all failed downloads"""
        if not self.failed_downloads_dict:
            messagebox.showinfo("Info", "No failed downloads to retry.")
            return
        
        if self.is_downloading:
            messagebox.showwarning("Warning", "Cannot retry while download is in progress. Please stop the download first.")
            return
        
        retry_count = len(self.failed_downloads_dict)
        self.log(f"Retrying {retry_count} failed downloads...")
        
        # Re-queue all failed downloads
        for remote_path, local_path in list(self.failed_downloads_dict.items()):
            # Remove from failed tracking
            if remote_path in self.failed_downloads:
                self.failed_downloads.remove(remote_path)
            
            # Remove from failed listbox
            for i in range(self.failed_listbox.size() - 1, -1, -1):  # Iterate backwards to avoid index issues
                if self.failed_listbox.get(i).startswith(remote_path):
                    self.failed_listbox.delete(i)
            
            # Re-queue for download
            self.download_queue.put((remote_path, local_path))
            # Track queued files
            with self.stats['lock']:
                if 'queued_files' not in self.stats:
                    self.stats['queued_files'] = 0
                self.stats['queued_files'] += 1
            
            # Re-add to treeview as pending if not already there
            if remote_path not in self.file_to_item:
                size = "Unknown"
                # Try to find size from file_list
                for fp, sz in self.file_list:
                    if fp == remote_path:
                        size = sz
                        break
                item_id = self.tree.insert("", tk.END, text=remote_path, values=(size, "Pending", ""))
                self.file_to_item[remote_path] = item_id
            else:
                # Update status to Pending
                item_id = self.file_to_item[remote_path]
                current_values = self.tree.item(item_id, 'values')
                if current_values:
                    size = current_values[0] if len(current_values) > 0 else "Unknown"
                    self.tree.item(item_id, values=(size, "Pending", ""))
                # Remove failed tag
                self.tree.item(item_id, tags=())
            
            # Remove from downloaded_paths so it can be retried
            with self.stats['lock']:
                if 'downloaded_paths' in self.stats:
                    self.stats['downloaded_paths'].discard(remote_path)
                # Reset stats for retry
                if remote_path in [err.split(':')[0] for err in self.stats.get('errors', [])]:
                    # Remove error for this file
                    self.stats['errors'] = [e for e in self.stats['errors'] if not e.startswith(f"{remote_path}:")]
        
        # Clear failed downloads dict
        self.failed_downloads_dict.clear()
        
        # Disable retry button
        self.retry_failed_button.config(state=tk.DISABLED)
        
        # Update failed count in stats
        with self.stats['lock']:
            # Adjust failed count (subtract retried files)
            self.stats['failed'] = max(0, self.stats['failed'] - retry_count)
        
        self.log(f"Queued {retry_count} files for retry. Click 'Start Download' to begin.")
        messagebox.showinfo("Retry", f"Queued {retry_count} failed downloads for retry. Click 'Start Download' to begin.")
    
    def _remove_from_treeview(self, remote_path):
        """Remove a file from the treeview"""
        if remote_path in self.file_to_item:
            item_id = self.file_to_item[remote_path]
            self.tree.delete(item_id)
            del self.file_to_item[remote_path]
            # Also remove from downloading items tracking and search tracking
            self.downloading_items_moved.discard(remote_path)
            if hasattr(self, 'all_tree_items'):
                self.all_tree_items.discard(item_id)
    
    def update_progress(self):
        """Update progress bar and stats"""
        if not self.is_downloading:
            return
        
        with self.stats['lock']:
            total = self.stats['total']
            completed = self.stats['completed']
            success = self.stats['success']
            failed = self.stats['failed']
        
        # Check if all workers are done, queue is empty, scanner is done, and no files are downloading
        queue_empty = self.download_queue.empty()
        all_done = all(not worker.is_alive() for worker in self.workers) if self.workers else False
        
        # Check if there are any files currently being downloaded or in queue
        with self.stats['lock']:
            downloading_count = len(self.stats.get('downloading_paths', set()))
            # Also check queue size (approximate, since we can't lock the queue)
            queue_size = self.download_queue.qsize()
        
        # Only mark as complete if ALL of these conditions are met:
        # 1. Scanner has finished discovering files (no more files will be added)
        # 2. Queue is completely empty (no files waiting to be processed)
        # 3. All workers are done (no active worker threads)
        # 4. No files are currently being downloaded (downloading_paths is empty)
        # 5. We have discovered at least some files
        # 6. All discovered files have been processed (completed >= total)
        is_complete = (self.scanner_done and 
                      queue_empty and 
                      queue_size == 0 and
                      all_done and 
                      downloading_count == 0 and 
                      total > 0 and 
                      completed >= total)
        
        # Calculate and update download speed
        current_time = time.time()
        speed = 0.0
        speed_str = "0 B/s"
        
        with self.stats['lock']:
            bytes_downloaded = self.stats.get('bytes_downloaded', 0)
            last_bytes = self.stats.get('last_bytes', 0)
            last_speed_time = self.stats.get('last_speed_time')
            
            # Initialize last_speed_time if not set
            if last_speed_time is None:
                last_speed_time = current_time
                self.stats['last_speed_time'] = current_time
                self.stats['last_bytes'] = 0
            
            # Calculate speed over last 2 seconds
            time_diff = current_time - last_speed_time
            if time_diff >= 2.0:  # Update speed every 2 seconds
                bytes_diff = bytes_downloaded - last_bytes
                speed = bytes_diff / time_diff if time_diff > 0 else 0
                self.stats['current_speed'] = speed
                self.stats['last_bytes'] = bytes_downloaded
                self.stats['last_speed_time'] = current_time
            else:
                # Use cached speed if not enough time has passed
                speed = self.stats.get('current_speed', 0)
        
        # Format speed (always show, even if 0)
        if speed >= 1024 * 1024:
            speed_str = f"{speed / (1024 * 1024):.1f} MB/s"
        elif speed >= 1024:
            speed_str = f"{speed / 1024:.1f} KB/s"
        else:
            speed_str = f"{speed:.0f} B/s"
        
        # Get total size and format it
        with self.stats['lock']:
            total_size = self.stats.get('total_size', 0)
            bytes_downloaded = self.stats.get('bytes_downloaded', 0)
        total_size_str = self._format_size(total_size) if total_size > 0 else "Unknown"
        
        # Calculate progress percentage
        progress_percent = 0.0
        if total_size > 0:
            progress_percent = (bytes_downloaded / total_size) * 100
        progress_str = f"{progress_percent:.1f}%" if total_size > 0 else "N/A"
        
        # Calculate ETA (time remaining)
        eta_str = "N/A"
        if speed > 0 and total_size > 0:
            remaining_bytes = total_size - bytes_downloaded
            if remaining_bytes > 0:
                eta_seconds = remaining_bytes / speed
                if eta_seconds < 60:
                    eta_str = f"{int(eta_seconds)}s"
                elif eta_seconds < 3600:
                    eta_str = f"{int(eta_seconds / 60)}m {int(eta_seconds % 60)}s"
                else:
                    hours = int(eta_seconds / 3600)
                    minutes = int((eta_seconds % 3600) / 60)
                    eta_str = f"{hours}h {minutes}m"
        
        # Calculate pending files (total - completed - failed)
        pending = max(0, total - completed - failed)
        
        # Update stats (always include speed, total size, progress, and ETA)
        self.stats_var.set(f"Files: {total} | Total Size: {total_size_str} | Progress: {progress_str} | ETA: {eta_str} | Completed: {completed} | Pending: {pending} | Failed: {failed} | Speed: {speed_str}")
        
        if is_complete:
            # Increment consecutive completion checks
            self.completion_checks_passed += 1
            
            # Require 5 consecutive successful checks (each 1 second apart) before final verification
            # This ensures downloads are truly done and gives time for any pending operations
            if self.completion_checks_passed >= 5:
                # Multiple checks passed, now do final verification
                self.root.after(2000, self._final_completion_check)  # Wait 2 more seconds before final check
            else:
                # Not enough consecutive checks yet, continue monitoring
                self.root.after(1000, self.update_progress)
        else:
            # Reset counter if not complete - any activity resets the count
            self.completion_checks_passed = 0
            # Schedule next update (always update status bar)
            self.root.after(500, self.update_progress)
    
    def _final_completion_check(self):
        """Final check to ensure download is really complete - verify multiple times with thorough checks"""
        if not self.is_downloading:
            return
        
        # Do multiple checks with delays to ensure nothing is still downloading
        def verify_complete(attempt=0, max_attempts=5):
            if not self.is_downloading:
                return
            
            # Check queue status
            queue_empty = self.download_queue.empty()
            queue_size = self.download_queue.qsize()
            
            # Check worker status
            all_done = all(not worker.is_alive() for worker in self.workers) if self.workers else False
            
            # Check downloading paths and stats
            with self.stats['lock']:
                downloading_count = len(self.stats.get('downloading_paths', set()))
                total = self.stats['total']
                completed = self.stats['completed']
                success = self.stats['success']
                failed = self.stats['failed']
                errors = self.stats['errors']
            
            # Additional check: see if any files in treeview are still downloading
            files_still_downloading = False
            try:
                for item_id in self.tree.get_children():
                    values = self.tree.item(item_id, 'values')
                    if values and len(values) > 1:
                        status = values[1]
                        if status and "Downloading" in status:
                            files_still_downloading = True
                            break
            except:
                pass
            
            # Comprehensive completion check
            is_still_complete = (self.scanner_done and 
                                queue_empty and 
                                queue_size == 0 and
                                all_done and 
                                downloading_count == 0 and 
                                not files_still_downloading and
                                total > 0 and 
                                completed >= total)
            
            if is_still_complete:
                if attempt < max_attempts - 1:
                    # Check again after a delay (longer delay for later attempts)
                    delay = 2000 if attempt >= 2 else 1000
                    self.root.after(delay, lambda: verify_complete(attempt + 1, max_attempts))
                else:
                    # All checks passed, we're truly done
                    # One final verification
                    final_queue_empty = self.download_queue.empty()
                    final_queue_size = self.download_queue.qsize()
                    final_all_done = all(not worker.is_alive() for worker in self.workers) if self.workers else False
                    
                    with self.stats['lock']:
                        final_downloading_count = len(self.stats.get('downloading_paths', set()))
                        final_total = self.stats['total']
                        final_completed = self.stats['completed']
                        final_success = self.stats['success']
                        final_failed = self.stats['failed']
                        final_errors = self.stats['errors']
                    
                    # One more check
                    if (self.scanner_done and 
                        final_queue_empty and 
                        final_queue_size == 0 and
                        final_all_done and 
                        final_downloading_count == 0 and 
                        final_total > 0 and 
                        final_completed >= final_total):
                        
                        # Now disable downloading and re-enable buttons
                        self.is_downloading = False
                        self.completion_checks_passed = 0
                        self.download_button.config(state=tk.NORMAL)
                        self.stop_button.config(state=tk.DISABLED)
                        self.test_connection_button.config(state=tk.NORMAL)
                        # Enable retry button if there are failed downloads
                        if self.failed_downloads_dict:
                            self.retry_failed_button.config(state=tk.NORMAL)
                        else:
                            self.retry_failed_button.config(state=tk.DISABLED)
                        # Enable retry button if there are failed downloads
                        if self.failed_downloads_dict:
                            self.retry_failed_button.config(state=tk.NORMAL)
                        else:
                            self.retry_failed_button.config(state=tk.DISABLED)
                        
                        # Calculate final speed
                        final_speed = 0.0
                        final_speed_str = "0 B/s"
                        final_time = time.time()
                        with self.stats['lock']:
                            final_bytes = self.stats.get('bytes_downloaded', 0)
                            start_time = self.stats.get('download_start_time')
                            if start_time:
                                elapsed = final_time - start_time
                                if elapsed > 0:
                                    final_speed = final_bytes / elapsed
                        
                        if final_speed >= 1024 * 1024:
                            final_speed_str = f"{final_speed / (1024 * 1024):.1f} MB/s"
                        elif final_speed >= 1024:
                            final_speed_str = f"{final_speed / 1024:.1f} KB/s"
                        else:
                            final_speed_str = f"{final_speed:.0f} B/s"
                        
                        # Get final total size and format it
                        with self.stats['lock']:
                            final_total_size = self.stats.get('total_size', 0)
                        final_total_size_str = self._format_size(final_total_size) if final_total_size > 0 else "Unknown"
                        
                        # Update stats one final time (include speed and total size)
                        # Calculate final progress
                        with self.stats['lock']:
                            final_total_size = self.stats.get('total_size', 0)
                            final_bytes_downloaded = self.stats.get('bytes_downloaded', 0)
                        final_progress_percent = 0.0
                        if final_total_size > 0:
                            final_progress_percent = (final_bytes_downloaded / final_total_size) * 100
                        final_progress_str = f"{final_progress_percent:.1f}%" if final_total_size > 0 else "N/A"
                        
                        final_pending = max(0, final_total - final_completed - final_failed)
                        self.stats_var.set(f"Files: {final_total} | Total Size: {final_total_size_str} | Progress: {final_progress_str} | Completed: {final_completed} | Pending: {final_pending} | Failed: {final_failed} | Speed: {final_speed_str}")
                        
                        if final_errors:
                            self.log(f"Download complete with {len(final_errors)} errors")
                        else:
                            self.log("Download complete!")
                        
                        # Only show dialog once
                        if not self.completion_dialog_shown:
                            self.completion_dialog_shown = True
                            # Show tray notification
                            self._show_tray_notification(
                                "Download Complete",
                                f"Completed: {final_completed}, Failed: {final_failed}",
                                duration=10
                            )
                            messagebox.showinfo("Complete", f"Download finished!\nCompleted: {final_completed}\nFailed: {final_failed}")
                    else:
                        # Final check failed, continue monitoring
                        self.completion_checks_passed = 0
                        self.update_progress()
            else:
                # Not complete, reset counter and continue monitoring
                self.completion_checks_passed = 0
                self.update_progress()
        
        # Start verification process
        verify_complete()
    
    def start_download(self):
        """Start recursive download of entire FTP server using multiple FTP connections"""
        local_dir = self.local_dir_entry.get().strip()
        if not local_dir:
            messagebox.showerror("Error", "Please specify a local directory.")
            return
        
        # Start recursive downloads immediately - no need to scan first
        self._start_parallel_downloads()
    
    def _start_parallel_downloads(self):
        """Start parallel recursive downloads using multiple FTP connections"""
        
        local_dir = self.local_dir_entry.get().strip()
        if not local_dir:
            messagebox.showerror("Error", "Please specify a local directory.")
            return
        
        os.makedirs(local_dir, exist_ok=True)
        
        host = self.host_entry.get().strip()
        port = int(self.port_entry.get() or 21)
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        remote_base = self.remote_path_entry.get().strip() or "/"
        use_tls = self.use_tls_var.get()
        num_threads = self.threads_var.get()
        
        # Reset stats
        with self.stats['lock']:
            self.stats['total'] = 0  # Will update as files are discovered
            self.stats['completed'] = 0
            self.stats['success'] = 0
            self.stats['failed'] = 0
            self.stats['errors'] = []
            self.stats['bytes_downloaded'] = 0
            self.stats['total_size'] = 0  # Total size of all files to download
            self.stats['queued_files'] = 0  # Count of files that have been queued
            self.stats['download_start_time'] = time.time()
            self.stats['last_bytes'] = 0
            self.stats['last_speed_time'] = time.time()
            self.stats['current_speed'] = 0.0
            # Shared sets to track downloaded and downloading paths across workers
            if 'downloaded_paths' not in self.stats:
                self.stats['downloaded_paths'] = set()
            if 'downloading_paths' not in self.stats:
                self.stats['downloading_paths'] = set()
        
        # Clear completed and failed listboxes
        self.completed_listbox.delete(0, tk.END)
        self.failed_listbox.delete(0, tk.END)
        self.completed_downloads = []
        self.failed_downloads = []
        self.failed_downloads_dict.clear()
        self.retry_failed_button.config(state=tk.DISABLED)
        # Reset search tracking
        if hasattr(self, 'all_tree_items'):
            self.all_tree_items.clear()
        
        # Reset completion tracking
        self.completion_dialog_shown = False
        self.completion_checks_passed = 0
        self.downloading_items_moved.clear()  # Reset downloading items tracking
        
        self.log(f"Starting recursive download with {num_threads} parallel download workers")
        num_scanners = self.scanners_var.get()  # Get scanner count from settings
        self.log(f"{num_scanners} scanners will discover files in parallel, {num_threads} workers will download in parallel")
        
        # Strategy: Multiple scanner threads discover files in parallel and queue them
        # Multiple download workers pull from queue and download
        # This speeds up file discovery while avoiding duplicate directory traversals
        
        # Reset scanner tracking
        self.scanned_dirs.clear()
        self.scanner_done = False
        with self.scanner_count_lock:
            self.scanner_count = 0
        
        # Try recursive LIST first for PureFTPd (much faster if supported)
        # DISABLED: The recursive listing parser is missing many files, so we'll rely on standard scanners
        # This will be attempted by the first scanner
        self.use_recursive_list = False  # Disabled - parser misses too many files
        self.recursive_list_attempted = False  # Track if we've tried it
        self.recursive_list_succeeded = False  # Track if recursive listing worked
        
        # Directory queue for parallel scanners to coordinate
        dir_queue = queue.Queue()
        dir_queue.put(remote_base)  # Start with base directory
        
        # Set downloading flag first
        self.is_downloading = True
        
        # Start download workers first (they'll wait for queue items)
        self.workers = []
        for i in range(num_threads):
            worker = DownloadWorker(i, self.download_queue, self.stats, host, port,
                                   local_dir, self.on_file_progress, self.update_file_status,
                                   username, password, use_tls, remote_base)
            worker.start()
            self.workers.append(worker)
            self.log(f"Download worker {i} started")
        
        # Start regular progress updates
        self.update_progress()
        
        # Start multiple scanner threads to discover files in parallel
        def scanner_thread(scanner_id):
            try:
                # Create ftputil connection using the same custom session factory
                if use_tls:
                    session_factory = create_no_utf8_session_factory(
                        base_class=ftplib.FTP_TLS,
                        port=port,
                        use_passive_mode=True,
                        encrypt_data_channel=True
                    )
                else:
                    session_factory = create_no_utf8_session_factory(
                        base_class=ftplib.FTP,
                        port=port,
                        use_passive_mode=True,
                        encrypt_data_channel=False
                    )
                
                scan_host = ftputil.FTPHost(host, username, password, session_factory=session_factory)
                
                with self.scanner_count_lock:
                    self.scanner_count += 1
                
                self.root.after(0, lambda sid=scanner_id: self.log(f"Scanner {sid} connected, discovering files..."))
                
                # Try recursive LIST for PureFTPd (only first scanner attempts this)
                # Use ftplib directly for this since it's a server-specific feature
                if scanner_id == 1 and self.use_recursive_list and not self.recursive_list_attempted:
                    self.recursive_list_attempted = True
                    # Create a temporary ftplib connection for recursive LIST
                    try:
                        if use_tls:
                            temp_ftp = ftplib.FTP_TLS()
                            temp_ftp.connect(host, port)
                            temp_ftp.login(username, password)
                            temp_ftp.prot_p()
                        else:
                            temp_ftp = ftplib.FTP()
                            temp_ftp.connect(host, port)
                            if username or password:
                                temp_ftp.login(username, password)
                        
                        if self._try_recursive_list(temp_ftp, remote_base, local_dir):
                            # Recursive listing succeeded
                            self.recursive_list_succeeded = True
                            self.root.after(0, lambda: self.log("Recursive listing completed, standard scanners will verify completeness."))
                        temp_ftp.quit()
                    except Exception as e:
                        # If recursive LIST fails, continue with standard scanning
                        pass
                
                # Process directories from queue
                while True:
                    
                    try:
                        # Get next directory with shorter timeout for faster response
                        current_path = dir_queue.get(timeout=0.5)
                    except queue.Empty:
                        # Check if we're done (no more directories and other scanners are done)
                        with self.scanner_count_lock:
                            if dir_queue.empty() and self.scanner_count <= 1:
                                # Last scanner, we're done
                                break
                            elif dir_queue.empty():
                                # Wait a bit and check again
                                continue
                    
                    # Scan this directory using ftputil
                    self._scan_and_queue_files_ftputil(scan_host, current_path, remote_base, local_dir, dir_queue)
                    dir_queue.task_done()
                
                # Add any remaining files to treeview
                if len(self.file_list) > 0:
                    remaining = self.file_list[len(self.file_list) - (len(self.file_list) % 20):]
                    if remaining:
                        self.root.after(0, lambda batch=remaining: self._batch_add_files_to_treeview(batch))
                
                scan_host.close()
                
                with self.scanner_count_lock:
                    self.scanner_count -= 1
                    if self.scanner_count == 0:
                        # Last scanner finished
                        self.root.after(0, lambda: self.log("All scanners finished discovering files"))
                        self.scanner_done = True
                        # Add poison pills to stop workers when queue is empty
                        for _ in range(num_threads):
                            self.download_queue.put(None)
                    else:
                        self.root.after(0, lambda sid=scanner_id: self.log(f"Scanner {sid} finished"))
                    
            except Exception as e:
                import traceback
                error_msg = str(e)
                traceback_str = traceback.format_exc()
                self.root.after(0, lambda sid=scanner_id, msg=error_msg: self.log(f"Scanner {sid} error: {msg}"))
                self.root.after(0, lambda: self.log(f"Traceback: {traceback_str}"))
                
                with self.scanner_count_lock:
                    self.scanner_count -= 1
                    if self.scanner_count == 0:
                        # Last scanner finished (even on error)
                        self.scanner_done = True
                        # Add poison pills to stop workers
                        for _ in range(num_threads):
                            self.download_queue.put(None)
        
        # Start multiple scanner threads (4 scanners for faster discovery)
        for i in range(num_scanners):
            threading.Thread(target=scanner_thread, args=(i+1,), daemon=True).start()
            self.log(f"Scanner {i+1} started")
        
        self.is_downloading = True
        self.download_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.test_connection_button.config(state=tk.DISABLED)
        
        # Start progress update
        self.update_progress()
    
    def _start_parallel_downloads_with_scan(self):
        """Start parallel downloads while scanning continues in background"""
        # This will scan and download simultaneously
        # Files will be added to queue as they're discovered
        local_dir = self.local_dir_entry.get().strip()
        host = self.host_entry.get().strip()
        port = int(self.port_entry.get() or 21)
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        remote_base = self.remote_path_entry.get().strip() or "/"
        use_tls = self.use_tls_var.get()
        num_threads = self.threads_var.get()
        
        # Build FTP URL base
        protocol = 'ftps' if use_tls else 'ftp'
        if username:
            if password:
                base_url = f"{protocol}://{quote(username)}:{quote(password)}@{host}:{port}"
            else:
                base_url = f"{protocol}://{quote(username)}@{host}:{port}"
        else:
            base_url = f"{protocol}://{host}:{port}"
        
        # Reset stats
        with self.stats['lock']:
            self.stats['total'] = 0  # Will update as files are found
            self.stats['completed'] = 0
            self.stats['success'] = 0
            self.stats['failed'] = 0
            self.stats['errors'] = []
        
        # Start worker threads
        self.workers = []
        for i in range(num_threads):
            worker = DownloadWorker(i, self.download_queue, self.stats, base_url,
                                   local_dir, self.on_file_progress, self.update_file_status,
                                   username, password)
            worker.start()
            self.workers.append(worker)
        
        self.is_downloading = True
        self.download_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.test_connection_button.config(state=tk.DISABLED)
        
        self.log(f"Started {num_threads} parallel wget instances, scanning and downloading simultaneously...")
        
        # Continue scanning and add files to queue as found
        def scan_and_queue():
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS()
                    ftp.connect(host, port)
                    ftp.login(username, password)
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP()
                    ftp.connect(host, port)
                    if username or password:
                        ftp.login(username, password)
                
                def scan_with_queue(ftp, current_path, base_path):
                    try:
                        if current_path != '/':
                            try:
                                ftp.cwd(current_path)
                            except:
                                return
                        
                        items = []
                        try:
                            for item in ftp.mlsd():
                                items.append(item)
                        except:
                            lines = []
                            ftp.retrlines('LIST', lines.append)
                            for line in lines:
                                parts = line.split()
                                if len(parts) >= 9:
                                    name = ' '.join(parts[8:])
                                    is_dir = parts[0].startswith('d')
                                    size = parts[4] if len(parts) > 4 else 'Unknown'
                                    items.append((name, {'type': 'dir' if is_dir else 'file', 'size': size}))
                        
                        for name, info in items:
                            if name in ['.', '..']:
                                continue
                            
                            remote_path = os.path.join(current_path, name).replace('\\', '/')
                            
                            if info.get('type') == 'dir':
                                scan_with_queue(ftp, remote_path, base_path)
                            else:
                                # Add to file list and queue immediately
                                size = info.get('size', 'Unknown')
                                self.file_list.append((remote_path, size))
                                
                                # Calculate local path and add to queue
                                if remote_path.startswith(remote_base):
                                    rel_path = remote_path[len(remote_base):].lstrip('/')
                                else:
                                    rel_path = remote_path.lstrip('/')
                                
                                local_path = os.path.join(local_dir, rel_path)
                                self.download_queue.put((remote_path, local_path))
                                # Track queued files
                                with self.stats['lock']:
                                    if 'queued_files' not in self.stats:
                                        self.stats['queued_files'] = 0
                                    self.stats['queued_files'] += 1
                                
                                # Update UI immediately
                                self.root.after(0, lambda p=remote_path, s=size: self._add_file_to_treeview(p, s))
                                
                                # Update stats
                                with self.stats['lock']:
                                    self.stats['total'] += 1
                                    # Add file size to total size (parse size - could be string or int)
                                    if isinstance(size, (int, float)):
                                        size_bytes = int(size)
                                    else:
                                        size_bytes = self._parse_size(size)
                                    self.stats['total_size'] += size_bytes
                                
                                if len(self.file_list) % 100 == 0:
                                    count = len(self.file_list)
                                    self.root.after(0, lambda c=count: self.log(f"Discovered {c} files, downloading in parallel..."))
                    except Exception as e:
                        self.root.after(0, lambda: self.log(f"Error scanning {current_path}: {str(e)}"))
                
                scan_with_queue(ftp, remote_base, remote_base)
                ftp.quit()
                
                self.root.after(0, lambda: self.log(f"Scan complete! Total files: {len(self.file_list)}"))
                
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Scan error: {str(e)}"))
        
        threading.Thread(target=scan_and_queue, daemon=True).start()
        
        # Start progress update
        self.update_progress()
    
    def update_progress(self):
        """Update progress bar and stats"""
        if not self.is_downloading:
            return
        
        with self.stats['lock']:
            total = self.stats['total']
            completed = self.stats['completed']
            success = self.stats['success']
            failed = self.stats['failed']
        
        # Progress bar removed - stats are shown in the Statistics frame
        
        # Calculate and display speed (even during scan)
        current_time = time.time()
        speed = 0.0
        speed_str = "0 B/s"
        
        with self.stats['lock']:
            bytes_downloaded = self.stats.get('bytes_downloaded', 0)
            last_bytes = self.stats.get('last_bytes', 0)
            last_speed_time = self.stats.get('last_speed_time')
            
            if last_speed_time is not None:
                time_diff = current_time - last_speed_time
                if time_diff >= 2.0:
                    bytes_diff = bytes_downloaded - last_bytes
                    speed = bytes_diff / time_diff if time_diff > 0 else 0
                    self.stats['current_speed'] = speed
                    self.stats['last_bytes'] = bytes_downloaded
                    self.stats['last_speed_time'] = current_time
                else:
                    speed = self.stats.get('current_speed', 0)
        
        # Format speed
        if speed >= 1024 * 1024:
            speed_str = f"{speed / (1024 * 1024):.1f} MB/s"
        elif speed >= 1024:
            speed_str = f"{speed / 1024:.1f} KB/s"
        else:
            speed_str = f"{speed:.0f} B/s"
        
        # Get total size and format it
        with self.stats['lock']:
            total_size = self.stats.get('total_size', 0)
            bytes_downloaded = self.stats.get('bytes_downloaded', 0)
        total_size_str = self._format_size(total_size) if total_size > 0 else "Unknown"
        
        # Calculate progress percentage
        progress_percent = 0.0
        if total_size > 0:
            progress_percent = (bytes_downloaded / total_size) * 100
        progress_str = f"{progress_percent:.1f}%" if total_size > 0 else "N/A"
        
        # Calculate ETA
        eta_str = "N/A"
        if speed > 0 and total_size > 0:
            remaining_bytes = total_size - bytes_downloaded
            if remaining_bytes > 0:
                eta_seconds = remaining_bytes / speed
                if eta_seconds < 60:
                    eta_str = f"{int(eta_seconds)}s"
                elif eta_seconds < 3600:
                    eta_str = f"{int(eta_seconds / 60)}m {int(eta_seconds % 60)}s"
                else:
                    hours = int(eta_seconds / 3600)
                    minutes = int((eta_seconds % 3600) / 60)
                    eta_str = f"{hours}h {minutes}m"
        
        # Calculate pending files (total - completed - failed)
        pending = max(0, total - completed - failed)
        self.stats_var.set(f"Files: {total} | Total Size: {total_size_str} | Progress: {progress_str} | ETA: {eta_str} | Completed: {completed} | Pending: {pending} | Failed: {failed} | Speed: {speed_str}")
        
        # Check if done
        if completed >= total and total > 0:
            self.is_downloading = False
            self.download_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.test_connection_button.config(state=tk.NORMAL)
            
            with self.stats['lock']:
                errors = self.stats['errors']
            
            if errors:
                self.log(f"Download complete with {len(errors)} errors")
            else:
                self.log("Download complete!")
            
            messagebox.showinfo("Complete", f"Download finished!\nCompleted: {completed}\nFailed: {failed}")
        else:
            # Schedule next update
            self.root.after(500, self.update_progress)
    
    def _start_recursive_wget_download(self):
        """Start recursive download using wget's built-in recursive mode"""
        local_dir = self.local_dir_entry.get().strip()
        if not local_dir:
            messagebox.showerror("Error", "Please specify a local directory.")
            return
        
        os.makedirs(local_dir, exist_ok=True)
        
        host = self.host_entry.get().strip()
        port = int(self.port_entry.get() or 21)
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        remote_path = self.remote_path_entry.get().strip() or "/"
        use_tls = self.use_tls_var.get()
        
        # Build FTP URL
        protocol = 'ftps' if use_tls else 'ftp'
        if username:
            if password:
                base_url = f"{protocol}://{quote(username)}:{quote(password)}@{host}:{port}"
            else:
                base_url = f"{protocol}://{quote(username)}@{host}:{port}"
        else:
            base_url = f"{protocol}://{host}:{port}"
        
        url = f"{base_url}{remote_path}"
        
        # Build wget command for recursive download
        cmd = [
            'wget',
            '--recursive',
            '--no-parent',
            '--no-host-directories',
            '--cut-dirs=0',
            '--no-verbose',
            '--progress=bar:force',
            '--no-check-certificate',
            '--continue',
            '--directory-prefix', local_dir,
            url
        ]
        
        self.log(f"Starting recursive download: {' '.join(cmd)}")
        
        def download_thread():
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1
                )
                
                self.download_process = process
                
                for line in process.stdout:
                    if not self.is_downloading:
                        process.terminate()
                        break
                    if line.strip():
                        self.root.after(0, lambda l=line.strip(): self.log(l))
                
                return_code = process.wait()
                
                if return_code == 0:
                    self.root.after(0, lambda: self.log("Recursive download complete!"))
                    self.root.after(0, lambda: messagebox.showinfo("Complete", "Download finished successfully!"))
                else:
                    self.root.after(0, lambda: self.log(f"Download finished with return code {return_code}"))
                    self.root.after(0, lambda: messagebox.showwarning("Warning", f"Download finished with return code {return_code}"))
                
            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda msg=error_msg: self.log(f"Error: {msg}"))
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Error", f"Download failed: {msg}"))
            finally:
                self.root.after(0, self._download_finished)
        
        self.is_downloading = True
        self.download_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.test_connection_button.config(state=tk.DISABLED)
        
        threading.Thread(target=download_thread, daemon=True).start()
    
    def _download_finished(self):
        """Called when download finishes"""
        self.is_downloading = False
        self.download_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.test_connection_button.config(state=tk.NORMAL)
        self.download_process = None
    
    def on_file_progress(self, worker_id, remote_path, percent):
        """Callback for file download progress"""
        # Progress bar removed - file status is shown in the treeview
        pass
    
    def _format_size(self, size_bytes):
        """Format file size into human-readable units"""
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f} KB"
        else:
            return f"{size_bytes} B"
    
    def _parse_size(self, size):
        """Parse file size from various formats (int, string, etc.) and return bytes"""
        if isinstance(size, int):
            return size
        if isinstance(size, str):
            if size == 'Unknown' or size == '':
                return 0
            # Try to parse as integer
            try:
                return int(size)
            except ValueError:
                return 0
        return 0
    
    def stop_download(self):
        """Stop downloading"""
        self.is_downloading = False
        
        # Stop all workers
        for worker in self.workers:
            worker.stop()
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=2)
        
        # Re-enable buttons
        self.download_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.test_connection_button.config(state=tk.NORMAL)
        # Enable retry button if there are failed downloads
        if self.failed_downloads_dict:
            self.retry_failed_button.config(state=tk.NORMAL)
        else:
            self.retry_failed_button.config(state=tk.DISABLED)
        
        self.log("Download stopped by user")
        
        # Stop wget process if running (legacy code, may not be needed)
        if self.download_process:
            try:
                self.download_process.terminate()
            except:
                pass
        
        # Stop all workers
        for worker in self.workers:
            worker.stop()
            self.download_queue.put(None)  # Poison pill
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=2)
        
        self.workers = []
        
        self.download_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.test_connection_button.config(state=tk.NORMAL)
        
        self.log("Download stopped")


def main():
    root = tk.Tk()
    app = FTPDownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

