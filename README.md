# Site Collector

Site Collector captures the current content and images of one-page WordPress/Elementor sites, so
they can be rebuilt later. For every website it saves the raw HTML, the Elementor CSS, every image
and a `manifest.json` describing the page's sections, menu, forms and images. It also writes a
`report.csv` covering the whole job.

It comes as a **desktop app** (macOS and Windows) and as a **command-line script** (`collect.py`).
Both run the same collector and produce the same output.

> **Client content must stay private.** Keep job folders out of public repos. If you commit a job
> to GitHub, make sure that repo is private. No credentials are needed or stored: only the public
> websites are collected.

## Desktop app

### Download

The app is built automatically by GitHub Actions (see [`build-app.yml`](.github/workflows/build-app.yml)).

- **Releases:** on the repo's **Releases** page, download the zip for your computer:
  - `Site-Collector-mac-apple-silicon.zip` for M1/M2/M3/M4 Macs
  - `Site-Collector-mac-intel.zip` for older Intel Macs
  - `Site-Collector-windows.zip`
- **Latest build:** the **Actions** tab → *Build desktop app* → the latest run → **Artifacts**.

A new release is published whenever a version tag (e.g. `v1.1.0`) is pushed. To build on demand,
go to **Actions → Build desktop app → Run workflow**.

### Install on a Mac

1. Unzip the file and drag **Site Collector** into **Applications**.
2. The app isn't signed with an Apple Developer certificate, so macOS blocks it the first time you
   open it:
   - Open it once and dismiss the warning. Then go to **System Settings → Privacy & Security**,
     scroll down and click **Open Anyway**.
   - If macOS instead says the app *"is damaged and can't be opened"*, run this once in Terminal:
     ```bash
     xattr -cr "/Applications/Site Collector.app"
     ```
3. From then on, open it like any other app.

On Windows, unzip the folder anywhere and run `Site Collector.exe`. If SmartScreen appears, click
**More info → Run anyway**.

### Using it

1. **Job folder → Choose…** Pick or create a folder for this job, e.g.
   `Documents/Site Collections/Acme`. Everything for the job is saved there: `sites.txt`, `sites/`,
   `report.csv` and `collect.log`. The app remembers the last folder you used.
2. **Websites:** paste the domains, one per line, or use **Import list…** to load a `.txt` or
   `.csv` file.
3. **Test run:** keep *Test run — only the first 3 sites* ticked for the first go, press **Start**,
   and check the results. Then untick it and press **Start** again to collect the rest. Sites that
   have already been collected are skipped.
4. **Results:** the table fills in as each site finishes. Green is OK, orange means the page is in
   a special state (maintenance, password, redirect), and red means the page couldn't be fetched.
   - Select one or more rows to **Open folder**, **View saved page** or **Open live site**.
   - **Collect again** re-collects the selected sites.
   - **Render in Chrome** re-collects them through a real browser. Use it for sites whose content
     looks incomplete; the app will suggest which ones.
   - Right-clicking a row gives the same options.
5. **Stop** finishes the current site and then stops. Press **Start** later to carry on where it
   left off.

**Options:**
- *Retry sites that had problems* only re-collects the sites whose last status wasn't OK.
- *Re-collect every site* starts over.

**Render in Chrome** uses the Google Chrome (or Microsoft Edge) already installed on the computer.

### Building the app yourself

A Mac app has to be built on a Mac (Windows on Windows):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-render.txt pyinstaller pillow
python packaging/build.py          # -> dist/Site Collector.app
```

## Command line (advanced)

### Setup (macOS)

```bash
cd LocalScraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional, only for the --render fallback:
pip install -r requirements-render.txt
python -m playwright install chromium
```

Run `source .venv/bin/activate` in each new terminal before using the script. You can also run the
desktop app from source with `python app.py`. It needs a Python that includes Tk: the python.org
installer does, and Homebrew needs `brew install python-tk`.

### Usage

Put the domains in `sites.txt`, one per line. The script ignores blank lines and lines that start
with `#`.

```bash
python collect.py --limit 3                       # first 3 sites in sites.txt (do this first)
python collect.py                                 # every site not collected yet
python collect.py --only example.com              # one site (repeat --only for more)
python collect.py --only example.com --force      # re-collect a site that's already collected
python collect.py --retry-failed                  # re-collect sites whose last status wasn't "ok"
python collect.py --only example.com --render     # add rendered.html (headless browser)
python collect.py --report-only                   # rebuild report.csv without fetching anything
```

| Option | What it does |
|---|---|
| `--only DOMAIN` | Collect only this domain. You can repeat it. |
| `--limit N` | Only process the first N domains in `sites.txt`. |
| `--force` | Re-collect sites that already have a `manifest.json`. Without it they're skipped. |
| `--retry-failed` | Re-collect only the sites whose last status wasn't `ok`. |
| `--render` | Also load the page in a headless browser, scroll to the bottom and save `rendered.html`. Re-collects the selected sites. Use it with `--only` for sites where `index.html` is clearly missing content. |
| `--report-only` | Rebuild `report.csv` from the existing manifests. |
| `--sites` / `--out` / `--report` | Input list, output folder and report path (default: next to the script). |
| `--workers N` | Number of parallel image downloads per site (default 4). |
| `--min-delay` / `--max-delay` | Pause between sites in seconds (default 1–2). |
| `-v` | Show debug output. The full log always goes to `collect.log`. |

A failure on one site never stops the run. At the end the script prints every site whose status
isn't `ok`, and lists any sites that look like they need `--render`.

When a site is re-collected, the new copy is built in a temporary folder and only then swapped in.
If a site with a good copy now fails outright (DNS, SSL or timeout), the previous copy is kept.

## Output

```
sites/<domain>/
  index.html      the HTML exactly as served
  rendered.html   only when --render was used
  manifest.json   the structure extracted from the page
  css/            Elementor/generated CSS (e.g. post-123.css)
  images/         every image found, with original filenames
report.csv        one row per domain in sites.txt
```

### Statuses

| Status | Meaning |
|---|---|
| `ok` | The page was collected normally. |
| `maintenance` | HTTP 503, coming-soon/maintenance plugin markup, or a "Coming soon"-style title. |
| `password_protected` | WordPress post password form, HTTP 401, or a redirect to a login page. |
| `redirect_offsite` | The domain redirects to a different domain. `www.` and `http→https` redirects don't count. The target page is still collected. |
| `blocked` | 403/429 or a Cloudflare/Sucuri/Wordfence challenge. Try `--render`. |
| `suspended` | The hosting "account suspended" page. |
| `http_error` | Any other 4xx/5xx response. |
| `dns_error` / `ssl_error` / `timeout` / `connection_error` / `redirect_loop` | The page couldn't be fetched. For `ssl_error` the script retries over plain `http://`, and if that works the content is still collected (the status stays `ssl_error`). |
| `error` | An unexpected problem. See `warnings` and `collect.log`. |
| `not_collected` | Only appears in `report.csv`: the domain hasn't been run yet. |

### manifest.json

The manifest follows the project brief, with a few extra fields that will help the next stage:

- **`sections`**: top-level Elementor sections/containers in page order, plus a theme
  header/footer if it wasn't built with Elementor.
  - `role`: `header` / `content` / `footer` / `popup`.
  - `anchor`: the element's `id`. If the section has no `id`, it's taken from an Elementor
    Menu Anchor widget inside it.
  - `anchor_ids`: every anchor target inside the section.
  - `anchor_only`: `true` when the section contains only a Menu Anchor widget. The anchor then
    really belongs to the next section.
  - `hidden_on`: Elementor responsive visibility, e.g. `["mobile"]`.
  - `has_nav`: whether the section contains the top navigation.
  - `images` and `background_images`: the images used inside the section. Background images
    come from inline styles, CSS rules targeting the section's elements, and Elementor
    `data-settings`.
- **`nav`**: the top navigation. `target` is the anchor id when the link points to the same page.
- **`forms`**: detected Elementor Forms, Contact Form 7, WPForms, Gravity Forms, Fluent Forms and
  Mailchimp forms. Each has `fields` (field names), `field_details` (label, type, required) and
  the `section` it's in.
- **`embeds`**: Google Maps, YouTube, Vimeo and other iframes, plus background/self-hosted
  videos. Videos are listed, not downloaded.
- **`images`**: one entry per image URL found on the page.
  - `found_in` is where the URL was first seen. `sources` lists every place it was seen:
    `img`, `srcset`, `data-src`, `style`, `css`, `css-inline`, `data-settings`, `data-attr`,
    `link` (lightbox/full-size links), `logo`, `favicon`, `og`, `json-ld`, and with `--render`
    also `computed-style` and `rendered-network`.
  - `element_ids` and `sections` show where the image is used on the page.
- **`stylesheets`**, **`generator`**, **`theme`**, **`elementor`** (page and template ids),
  **`http_status`** and **`redirects`**.

### How images are handled

- WordPress resized copies such as `photo-300x200.jpg` are traced back to the original
  `photo.jpg`. If the original doesn't exist, the largest variant found on the page is
  downloaded instead. All the variants of one image are saved as a single file.
- The script counts distinct images, so `images_found` in `report.csv` counts each image once,
  not once per resized variant.
- Filename clashes (e.g. `2023/05/team.jpg` and `2024/02/team.jpg`) get a numeric suffix:
  `team.jpg`, `team-2.jpg`. The check ignores case, because macOS filenames are case-insensitive.
- Skipped: data URIs, tracking pixels (Facebook, Google, LinkedIn, etc.), lazy-load placeholder
  GIFs and WordPress core images. SVGs are kept.
- A response that isn't actually an image, such as a "soft 404" HTML page, counts as a failure
  and isn't saved.
- Stylesheets are downloaded from `/wp-content/uploads/` (Elementor `post-*.css`) and from cache
  folders (WP Rocket, Autoptimize, LiteSpeed). Plugin and theme stylesheets aren't downloaded.
  Every `url(...)` is resolved against the CSS file's own URL.

## Acceptance checks

1. **Try 3 sites first:** use the app's test run, or `python collect.py --limit 3`. Then look at
   `report.csv` and `sites/*/manifest.json`.
2. **Text is all there:** open a few random `index.html` files in a browser. In the app, select
   a row and click **View saved page**. Styling may look broken offline, which is fine. The text
   should all be there.
   ```bash
   ls sites | sort -R | head -3 | while read d; do open "sites/$d/index.html"; done
   ```
3. **Image counts match the live page:** list the images per section for a site, then compare
   with the live page:
   ```bash
   python3 -c "import json,sys; m=json.load(open(sys.argv[1]));
   [print(s['order'], s['role'], s['anchor'], '| images', len(s['images']), '| backgrounds', len(s['background_images']), '|', s['headings'][:1]) for s in m['sections']]" sites/example.com/manifest.json
   ```
4. **Every domain is reported:** `report.csv` has one row for every domain in `sites.txt`, and the
   run ends by listing the rows that aren't `ok`.
5. **Commit and push to the private repo.**

## Repo size

Eighty sites' worth of original-size images can add up to several GB. GitHub rejects single files
over 100 MB and warns above 50 MB, and the script skips images over 50 MB. If the repo gets
large, consider [Git LFS](https://git-lfs.com) for `sites/**/images/**` before the first
content commit.

## Tests

```bash
pip install -r requirements-render.txt   # the render test is skipped if Playwright isn't installed
python -m unittest discover -s tests -v
```

GitHub Actions runs the tests before every app build. Each build also runs the packaged app's
self-test (`--self-test --require-browser`), which checks that it can launch a browser and render
a page.

The tests serve a realistic mock Elementor site locally, plus maintenance, password-protected,
redirecting, JavaScript-rendered and unresolvable sites. They then check the manifest, the image
files and the report.
