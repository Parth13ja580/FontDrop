# FontDrop - ZIP Font Installer

Hey guys. I built FontDrop because dealing with massive font packs is a pain. Instead of extracting folders, digging through files, and installing fonts one by one, you can just drag and drop the whole ZIP file into this app. 

### What it does:
* **Drag & Drop ZIPs:** Throw one or multiple ZIP files straight into the app. It scans them instantly.
* **Live Previews:** Hover over the eye icon to see exactly what the font looks like before you clutter your system.
* **One-Click Install:** Select the ones you want and hit install. It handles the Windows registry stuff in the background.
* **Manage Installed Fonts:** Search through what’s already on your PC and uninstall the garbage you don't need.
* **History:** Keeps a log of what you installed and when.

---

### How to Use
Literally just double-click `FontDrop.exe`. 

*Note: It will pop up a Windows Admin prompt (UAC) asking for permission or if it doesn't prompt just run it as administrator.*

---

### Troubleshooting (If things look weird or crash)

Since this app wraps a modern web UI inside a desktop window, it relies on a couple of standard Windows components. If you are on a brand-new PC or an older version of Windows, you might run into these:

**1. The app looks like a broken, ugly website from 90s**
Your Windows is missing the modern UI engine (WebView2), so it panicked and fell back to Internet Explorer. 
* **The Fix:** Download and install the "WebView2 Evergreen Bootstrapper" from Microsoft's official site. 

**2. The app crashes instantly or says "Missing DLL"**
Your PC is missing the base C++ libraries that Python apps need to run.
* **The Fix:** Download and install the "Visual C++ 2015-2022 Redistributable" from Microsoft.

**3. Fonts say "Failed" or "Permission Denied"**
You didn't run the app as an Administrator. Close it, right-click `FontDrop.exe`, and select "Run as Administrator".

---

### For Geeks: It won't run? Recompile it yourself.

If the `.exe` absolutely refuses to work on your specific setup (for example, if you are stubbornly still using Windows 7), you can just build the `.exe` yourself natively on your machine. 

1. Open the `raw_file` folder included in this download.
2. Make sure you have Python installed on your PC.
3. Open your terminal or command prompt in that folder and install the requirements:
   `pip install pywebview pyinstaller`
4. Run this exact command to compile your own `.exe`:
   `pyinstaller --noconfirm --onefile --windowed --name FontDrop --manifest fontdrop.manifest font_installer_app.py`

The fresh `.exe` will pop out in the `dist` folder.