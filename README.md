# ftp donloader

A multi-threaded GUI FTP downloader built with Python and tkinter. Perfect for archiving entire FTP servers with fast parallel downloads and real-time file discovery.

## Features

- **Multi-threaded Downloads**: Uses multiple parallel worker threads with Python's `ftplib` for fast concurrent downloads
- **Real-time File Discovery**: Files appear in the download list as they're discovered, with 4 parallel scanners for optimal speed
- **Optimized Scanning**: Prioritizes faster FTP listing methods (MLSD, NLST) and includes PureFTPd-specific optimizations (`LIST -R`)
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
- tkinter (usually included with Python)
- Pillow (optional, for status images and icon support)

## Installation

1. Install Python 3.6 or higher if you haven't already

2. (Optional) Install Pillow for image support:
   ```bash
   pip install Pillow
   ```
   The application will work without Pillow, but status images won't be displayed.

3. No other dependencies required - the application uses only Python standard library modules:
   - `tkinter` (GUI)
   - `ftplib` (FTP operations)
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

4. Adjust the number of download threads (default: 4)

5. Click **"Start Download"** to begin:
   - The application will automatically discover files as it scans
   - Files appear in the "Files to Download" list in real-time
   - Currently downloading files move to the top of the list
   - Completed files are moved to the "Completed Downloads" listbox
   - Failed files are moved to the "Failed Downloads" listbox with error details

6. Monitor progress in the statistics panel and log area

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
   - Uses 4 parallel scanner threads to discover files
   - Prioritizes faster FTP listing methods: `MLSD` → `NLST` → `LIST`
   - For PureFTPd servers, attempts `LIST -R` for faster recursive scanning
   - Files are added to the download queue and UI as they're discovered

2. **Download Phase**: 
   - Creates multiple worker threads (configurable, default: 4)
   - Each worker maintains its own FTP connection
   - Files are distributed to available workers via a queue system
   - Duplicate downloads are prevented by tracking downloaded/downloading files

3. **Progress Tracking**: 
   - Monitors completion status and updates the GUI in real-time
   - Calculates and displays overall download speed
   - Shows per-file status and speed
   - Automatically detects completion when all files are downloaded

## Tips

- **Thread Count**: More threads = faster downloads, but too many may overwhelm the server. Start with 4-8 threads.
- **Large Servers**: For very large FTP servers, file discovery happens in real-time, so you can start monitoring immediately.
- **PureFTPd Servers**: The application automatically detects and optimizes for PureFTPd servers using `LIST -R` for faster scanning.
- **Resume**: If a download is interrupted, you can restart it - the application will skip files that already exist locally.
- **File Navigation**: Currently downloading files automatically appear at the top of the list for easy monitoring.

## Troubleshooting

- **Connection errors**: Use the "Test Connection" button to verify your FTP server address, port, and credentials
- **Slow downloads**: Try adjusting the number of threads or check your network connection
- **Missing images**: Install Pillow (`pip install Pillow`) if status images aren't displaying
- **Files not appearing**: Check the log area for scanner errors or connection issues

## License

This project is provided as-is for personal use.
