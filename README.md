# ftp donloader

A multi-threaded GUI FTP downloader built with Python and tkinter. Perfect for archiving entire FTP servers with fast parallel downloads and real-time file discovery.

## Features

- **Multi-threaded Downloads**: Uses multiple parallel worker threads with `ftputil` for fast concurrent downloads
- **Timestamp Preservation**: Automatically preserves file modification timestamps from the FTP server
- **Real-time File Discovery**: Files appear in the download list as they're discovered, with 4 parallel scanners for optimal speed
- **Optimized Scanning**: Uses `ftputil`'s `listdir` API and includes PureFTPd-specific optimizations (`LIST -R`) for faster scanning
- **Test Connection**: Test FTP connectivity and retrieve server information before downloading
- **Duplicate Prevention**: Automatically skips files that are already downloaded or currently downloading
- **Progress Tracking**: Real-time statistics including download speed, completed/failed counts, and per-file status
- **Status Indicators**: Visual status images (success/failed) in the file list
- **Organized Results**: Separate listboxes for completed and failed downloads
- **Smart UI**: Currently downloading files automatically move to the top of the list for easy monitoring
- **FTP/FTPS Support**: Works with both regular FTP and secure FTPS (TLS/SSL) connections
- **User-friendly GUI**: Clean and intuitive interface built with tkinter

## Requirements

- Python 3.6 or higher
- `ftputil` (required, for FTP operations with timestamp preservation)
- `tkinter` (usually included with Python)
- `Pillow` (optional, for status images and icon support)

## Installation

1. Install Python 3.6 or higher if you haven't already

2. Install required dependencies:
   ```bash
   pip install ftputil
   ```
   Or install all dependencies including optional ones:
   ```bash
   pip install -r requirements.txt
   ```

3. (Optional) Install Pillow for image support:
   ```bash
   pip install Pillow
   ```
   The application will work without Pillow, but status images won't be displayed.

4. The application also uses Python standard library modules:
   - `tkinter` (GUI)
   - `threading` (parallel downloads and scanning)
   - `queue` (task management)
   - `os`, `shutil`, `re`, `time`, `pathlib`, `urllib.parse`

## Usage

1. Run the application:
   ```bash
   python ftp_downloader.py
   ```

2. Enter FTP connection details:
   - **Host**: FTP server address (e.g., `modland.com`)
   - **Port**: FTP port (default: 21)
   - **Username**: FTP username (e.g., `anonymous`)
   - **Password**: FTP password (leave empty for anonymous)
   - **Use TLS/SSL**: Check if using FTPS
   - **Remote Path**: Starting directory on FTP server (e.g., `/`)
   - **Local Directory**: Where to save downloaded files

3. (Optional) Click **"Test Connection"** to verify connectivity and view server information

4. Adjust download settings:
   - **Number of Threads**: Number of parallel download workers (default: 4)
   - **Number of Scanners**: Number of parallel file discovery scanners (default: 4)
   - **Auto-retry failed downloads**: Automatically retry failed downloads during the download process

5. Click **"Start Download"** to begin:
   - The application will automatically discover files as it scans
   - Files appear in the "Files to Download" list in real-time
   - Currently downloading files move to the top of the list
   - Completed files are moved to the "Completed Downloads" listbox
   - Failed files are moved to the "Failed Downloads" listbox with error details

6. Monitor progress in the statistics panel and log area

7. **Retry Failed Downloads**: 
   - If downloads fail, use the **"Retry Failed"** button to re-queue all failed downloads
   - Or enable **"Auto-retry failed downloads"** to automatically retry failures during download

## Example: Downloading from modland.com

1. Set Host to: `modland.com`
2. Set Username to: `anonymous`
3. Leave Password empty
4. Set Remote Path to: `/` (or a specific subdirectory)
5. Click "Test Connection" to verify the connection
6. Click "Start Download" to begin downloading
7. Watch files appear in real-time as they're discovered

## How It Works

1. **Scanning Phase**: 
   - Uses 4 parallel scanner threads with `ftputil` to discover files
   - Uses `ftputil`'s `listdir()` API for clean directory listing
   - For PureFTPd servers, attempts `LIST -R` for faster recursive scanning
   - Files are added to the download queue and UI as they're discovered

2. **Download Phase**: 
   - Creates multiple worker threads (configurable, default: 4)
   - Each worker maintains its own `ftputil.FTPHost` connection
   - Files are distributed to available workers via a queue system
   - Downloads preserve file modification timestamps from the server
   - Duplicate downloads are prevented by tracking downloaded/downloading files

3. **Progress Tracking**: 
   - Monitors completion status and updates the GUI in real-time
   - Statistics panel shows: Files, Total Size, Progress percentage, ETA, Completed count, Pending count, Failed count, and current Speed
   - Shows per-file status and speed in the file list
   - Automatically detects completion when all files are downloaded

## Tips

- **Thread Count**: More threads = faster downloads, but too many may overwhelm the server. Start with 4-8 threads.
- **Scanner Count**: More scanners = faster file discovery. For very large servers (100k+ files), 4-6 scanners work well. Too many may overwhelm the server.
- **Large Servers**: For very large FTP servers, file discovery happens in real-time, so you can start monitoring immediately.
- **Retry Failed Downloads**: If some downloads fail, use the "Retry Failed" button after the download completes, or enable auto-retry to retry failures automatically.
- **Resume**: If a download is interrupted, you can restart it - the application will skip files that already exist locally.
- **File Navigation**: Currently downloading files automatically appear at the top of the list for easy monitoring.

## Troubleshooting

- **Connection errors**: Use the "Test Connection" button to verify your FTP server address, port, and credentials
- **"504 Unknown command" errors**: Some FTP servers don't support UTF-8 encoding. The application automatically handles this with a custom session factory
- **Slow downloads**: Try adjusting the number of threads or check your network connection
- **Missing images**: Install Pillow (`pip install Pillow`) if status images aren't displaying
- **Files not appearing**: Check the log area for scanner errors or connection issues
- **Installation issues**: Make sure `ftputil` is installed: `pip install ftputil`

## License

This project is provided as-is for personal use.
