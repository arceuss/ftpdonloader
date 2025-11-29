# Multi-threaded FTP Downloader GUI

A GUI application that uses multiple parallel `wget` instances to download files from FTP servers. Perfect for archiving entire FTP servers with much faster download speeds than a single instance.

## Features

- **Multi-threaded Downloads**: Uses multiple parallel `wget` instances for faster downloads
- **FTP Server Scanning**: Scans FTP servers to build a file list before downloading
- **Progress Tracking**: Real-time progress bar and statistics
- **Resume Support**: Automatically resumes interrupted downloads
- **FTP/FTPS Support**: Works with both regular FTP and secure FTPS connections
- **User-friendly GUI**: Clean and intuitive interface built with tkinter

## Requirements

- Python 3.6 or higher
- `wget` installed and available in your system PATH
- tkinter (usually included with Python)

## Installation

1. Ensure `wget` is installed and in your PATH:
   - **Windows**: Download from [GNU Wget for Windows](https://eternallybored.org/misc/wget/)
   - **Linux**: Usually pre-installed, or install via `sudo apt-get install wget` (Debian/Ubuntu) or `sudo yum install wget` (RHEL/CentOS)
   - **macOS**: Install via Homebrew: `brew install wget`

2. No Python packages need to be installed - the application uses only standard library modules.

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

3. Click **"Scan FTP Server"** to discover all files on the server

4. Adjust the number of parallel threads (default: 4)

5. Click **"Start Download"** to begin downloading with multiple parallel `wget` instances

6. Monitor progress in the progress bar and log area

## Example: Downloading from modland.com

1. Set Host to: `modland.com`
2. Set Username to: `anonymous`
3. Leave Password empty
4. Set Remote Path to: `/` (or a specific subdirectory)
5. Click "Scan FTP Server" to see all available files
6. Click "Start Download" to download everything with parallel instances

## How It Works

1. **Scanning Phase**: Uses Python's `ftplib` to recursively scan the FTP server and build a complete file list
2. **Download Phase**: Creates multiple worker threads, each running a separate `wget` process to download files in parallel
3. **Queue Management**: Files are distributed to available workers via a queue system
4. **Progress Tracking**: Monitors completion status and updates the GUI in real-time

## Tips

- **Thread Count**: More threads = faster downloads, but too many may overwhelm the server. Start with 4-8 threads.
- **Large Servers**: For very large FTP servers, scanning may take some time. Be patient!
- **Resume**: If a download is interrupted, you can restart it - `wget` will automatically resume partial downloads.

## Troubleshooting

- **"wget not found"**: Ensure `wget` is installed and in your system PATH
- **Connection errors**: Check your FTP server address, port, and credentials
- **Slow downloads**: Try adjusting the number of threads or check your network connection

## License

This project is provided as-is for personal use.

"# ftpdonloader" 
