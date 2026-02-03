# Fixing 403 Errors in YouTube Downloads

## What was fixed

Your `/download` and `/download/direct` endpoints now have enhanced handling for YouTube's 403 "Forbidden" errors. These are caused by YouTube's bot detection systems blocking the requests.

## Solutions implemented

### 1. **Enhanced HTTP Headers**

- Modern User-Agent string (Chrome 120)
- Complete set of browser-like headers (Accept, Accept-Language, Accept-Encoding, DNT, etc.)
- Keep-alive connections
- Proper encoding headers

### 2. **Cookie-based Authentication** ✅

- **Automatically enabled**: Code automatically loads `cookies.txt` from the project root
- Your `cookies.txt` from Edge's Cookie Editor extension is already in use
- No additional configuration needed - just keep it in the root folder
- Cookies provide persistent authentication to bypass YouTube bot detection

### 3. **Retry Logic**

- `retries: 10` - Retries on network errors
- `fragment_retries: 3` - Retries individual fragments
- `file_access_retries: 10` - Handles file access errors
- `skip_unavailable_fragments: True` - Gracefully handles unavailable fragments

### 4. **Better Format Selection**

- Simplified format: `bestaudio/best` with maximum compatibility
- Automatically selects best available audio format
- Falls back to video+audio if audio-only unavailable
- FFmpegExtractAudio automatically extracts audio from any stream

### 5. **Extractor Configuration**

- Uses Android and Web player clients for better compatibility
- Bypasses YouTube's "n" parameter challenge protection
- Language preference set to English
- Optimized for audio extraction

### 6. **JavaScript Runtime Support**

YouTube's "n" parameter challenge requires JavaScript execution. yt-dlp handles this automatically if you have Node.js or Deno installed.

**Already installed on your system:**

- Deno (you mentioned it was downloaded alongside yt-dlp)

This allows yt-dlp to solve YouTube's JavaScript challenges automatically.

## How to refresh cookies (if needed)

If downloads fail with 403 errors, your cookies may be expired. Here's how to export fresh cookies from Edge using the Cookie Editor extension:

### From Edge Browser (Cookie Editor Extension):

1. Open Edge and go to YouTube (youtube.com)
2. Open Cookie Editor extension
3. Click "Export" → Choose Netscape format
4. Save the exported file as `cookies.txt` in the project root (replace the old one)
5. Restart the API

### Using yt-dlp CLI with cookies (from project root):

```bash
yt-dlp --cookies cookies.txt -f bestaudio -x --audio-format mp3 "https://www.youtube.com/watch?v=VIDEO_ID"
```

Or to export fresh cookies directly from Edge:

```bash
yt-dlp --cookies-from-browser edge --cookies cookies.txt "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
```

These commands:

- `--cookies cookies.txt` - Explicitly uses your cookies.txt file
- `--cookies-from-browser edge` - Reads cookies from Edge's storage
- `-f bestaudio` - Downloads best audio quality
- `-x --audio-format mp3` - Converts to MP3

**Note:** The API automatically picks up updated cookies.txt without needing a restart.

## Error handling

The endpoints now properly distinguish between different error types:

- **403 Forbidden**: YouTube blocked the request (expired cookies, throttling)
  - Solution: Refresh cookies.txt using Edge Cookie Editor
- **429 Rate Limited**: YouTube is rate-limiting requests
  - Solution: Wait and retry later, or use /download/direct endpoint
- **"n challenge solving failed"**: JavaScript runtime issue
  - Already handled: Your Deno installation allows yt-dlp to solve these challenges
  - Code now uses `player_client: ['android', 'web']` to bypass challenges
- **"Requested format is not available"**: Format selection issue
  - Fixed: Now uses flexible `bestaudio/best` format
- **Other Errors**: Extraction or network issues
  - Check logs for specific error messages

## Troubleshooting

### If you still get "n challenge solving failed":

1. **Verify Deno is accessible** (already installed):

   ```bash
   deno --version
   ```

2. **Update yt-dlp to latest version**:

   ```bash
   pip install -U yt-dlp
   ```

3. **The code now uses Android player client** which bypasses most challenges

### If downloads fail:

1. First, try the `/download/direct` endpoint (uses multiple fallback methods)
2. Check if cookies.txt is valid and not expired
3. Refresh cookies using the methods above
4. Check logs for specific error messages

## Environment Variables

No new environment variables needed. The system automatically uses:

- `cookies.txt` - Located in project root, from Edge Cookie Editor extension
- Built-in ffmpeg from `bin/` directory
- Cache directory at `downloads/`

## How it works internally

The code in `api/main.py` automatically handles cookies:

```python
# From get_yt_dlp_options() function
cookies_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'cookies.txt')

if os.path.exists(cookies_file):
    opts['cookiefile'] = cookies_file  # yt-dlp loads cookies from this file
```

Your Edge Cookie Editor cookies are in Netscape format, which yt-dlp reads natively.
