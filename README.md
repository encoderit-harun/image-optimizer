# Image Optimizer

**Developed by Harun from Encoder IT Limited**

**GitHub:** https://github.com/encoderit-harun/image-optimizer

Reduces the file size of JPG, JPEG, PNG and WebP images. It can also convert images to WebP or AVIF.
Everything runs on your own computer. No image is ever uploaded to the internet.

---

## 1. Starting the server

### Mac
1. Open the `image-optimizer` folder.
2. Double-click **`start.command`**.
3. A Terminal window opens, and after a moment the app opens in your browser automatically.

> **If macOS says "cannot be opened because it is from an unidentified developer" the first time:**
> **Right-click `start.command` → Open → Open**. You only need to do this once.

### Windows
Double-click **`start.bat`** in the `image-optimizer` folder.

### From a terminal (any system)
```bash
cd path/to/image-optimizer
.venv/bin/python app.py          # Windows: .venv\Scripts\python app.py
```

### Opening the app
- If the browser doesn't open by itself, go to: **http://127.0.0.1:8765/**
- Closing the browser tab is fine while the server is running. Just open the link above again.

### Stopping the server
Press **Ctrl + C** in the Terminal window, or simply close the window.
**Note:** Closing the Terminal window stops the app. Keep it open while you work.

### First run
- **On the first run, or after moving the folder somewhere else,** `start.command` / `start.bat` sets everything up automatically if needed.
- **Internet is needed only once**, to download the Pillow library. Setup takes about a minute. After that it works offline.
- **Python 3.9 or newer is required.** It is usually already installed on a Mac. If not, install it from: https://www.python.org/downloads/

---

## 2. Moving the folder

You can keep the whole **`image-optimizer`** folder anywhere: Desktop, Documents, or another drive.

- **Move the whole folder together.** Don't move individual files out of it: the app won't run unless `app.py`, `optimizer.py` and the `static/` folder stay together.
- **Images are always saved in the same place.** Wherever the folder lives, optimized images go to an `OptimizedImages_<date-time>` folder inside your **Downloads** folder (see below).

---

## 3. How to use

1. Choose images with **Select Images** or **Select Folder**, or drag and drop images or folders onto the page.
   Only `.jpg`, `.jpeg`, `.png` and `.webp` files are accepted. Other files and hidden files (such as `.DS_Store`) are skipped.

   **When you select a folder:**
   - Only images in the **top-level folder** are included. Images in sub-folders (e.g. thumbnail folders) are skipped.
   - To include sub-folder images too, tick **"Include images in sub-folders"**.
   - Chrome may ask something like "Upload 463 files to this site?". This is Chrome's own warning. The count includes sub-folders and all other files, and the files only go to this app on your own computer. Click **Upload**.
2. Choose a mode:
   - **Convert format unticked:** the format stays the same, only the size is reduced (`photo.jpg` → `photo.jpg`).
   - **Convert format ticked:** images are converted to **WebP** (default) or **AVIF**, and their size is reduced too.
3. Set the **Maximum output size** (default 3 MB). Leave the box empty for no limit.
4. Click **Start Optimization**.

---

## 4. Where are images saved? Are they downloaded automatically?

**Yes, images are saved automatically.** Each image is saved as soon as it finishes, here:

```
Downloads/
└── OptimizedImages_2026-11-17_10-58-32/     ← a new folder with the date and time on every run
    ├── photo.jpg
    ├── logo.webp
    └── ...
```

- **No nested folders.** Even when you select a folder, the images go straight into `OptimizedImages_<date-time>`. Only when "Include images in sub-folders" is ticked do sub-folder images keep their own sub-folders, so files with the same name don't collide.
- **No button to press.** When processing finishes, the files are already in Downloads.
- **Open folder** opens that folder.
- **View** shows the image in the browser.
- **Download all (ZIP)** gives you all images in one ZIP file, handy for sending to someone.
- **Your originals are never changed.** Original images are never modified or deleted. New files are always created.
- **To save somewhere other than Downloads**, set `IMAGE_OPTIMIZER_OUTPUT` before starting the app. The `OptimizedImages_<date-time>` folder will then be created there. Example:
  `IMAGE_OPTIMIZER_OUTPUT=~/Desktop .venv/bin/python app.py`

---

## 5. How compression works

| Output | Encoder (inside Pillow) | Normal quality | Only if needed to fit 3 MB |
|---|---|---|---|
| JPEG | libjpeg-turbo | 85 | 80 → 75 |
| WebP | libwebp | 82 | 77 → 72 |
| AVIF | libavif | 64 | 58 → 52 |
| PNG | zlib + 256-colour palette | lossless or palette | palette |

- **Quality first.** Most images finish in a single pass. If the file is still too large, quality is lowered a little at a time.
- **Resizing is a last resort.** If that still isn't enough, the image is scaled down slightly, but never below 60% or below 2000 px on the long side.
- **If the limit can't be reached**, the best-quality version is kept, with the message: *"Could not reach 3 MB without significant quality loss."*
- **What is preserved:** transparency, orientation, colours and aspect ratio. Images are never upscaled.

---

## 6. Files

```
image-optimizer/
├── start.command     Mac: double-click to start
├── start.bat         Windows: double-click to start
├── app.py            Local server (only reachable from this computer)
├── optimizer.py      Compression engine
├── static/index.html The app page
├── static/brand/     Encoder IT Limited logo and favicon
├── requirements.txt  Required libraries (pillow>=11.3)
└── .venv/            Python environment (created automatically)
```

---

<p align="center">
  Developed by <b>Harun</b> from <b>Encoder IT Limited</b><br>
  <a href="https://github.com/encoderit-harun/image-optimizer">github.com/encoderit-harun/image-optimizer</a><br>
  © 2026 Encoder IT Limited. All rights reserved.
</p>
