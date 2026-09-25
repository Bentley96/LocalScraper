# Site collection

This repo captures the current content and images of the customer's one-page WordPress/Elementor
sites. The collected content will later be mapped to a shared section model. This stage only
collects: it doesn't redesign anything or make any changes in WordPress.

> **This repo holds client content and must be private.** Check that it's private on GitHub
> (Settings → General → Danger Zone → Change visibility) before committing anything under `sites/`.
> No credentials are needed or stored anywhere. Only the public sites are collected.

## Setup (macOS)

```bash
cd LocalScraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional, only for the --render fallback:
pip install -r requirements-render.txt
python -m playwright install chromium
```

Run `source .venv/bin/activate` in each new terminal before using the script.

## Usage

Put the domains in `sites.txt`, one per line. The script ignores blank lines and lines that start
with `#`.

```bash
python collect.py --limit 3                       # first 3 sites in sites.txt (do this first)
python collect.py                                 # every site not collected yet
python collect.py --only example.com              # one site (repeat --only for more)
python collect.py --only example.com --force      # re-collect a site that's already collected
python collect.py --retry-failed                  # re-collect sites whose last status wasn't "ok"
python collect.py --only example.com --render     # add rendered.html (headless Chromium)
python collect.py --report-only                   # rebuild report.csv without fetching anything
```

| Option | What it does |
|---|---|
| `--only DOMAIN` | Collect only this domain. You can repeat it. |
| `--limit N` | Only process the first N domains in `sites.txt`. |
| `--force` | Re-collect sites that already have a `manifest.json`. Without it they're skipped. |
| `--retry-failed` | Re-collect only the sites whose last status wasn't `ok`. |
| `--render` | Also load the page in headless Chromium, scroll to the bottom and save `rendered.html`. Re-collects the selected sites. Use it with `--only` for sites where `index.html` is clearly missing content. |
| `--report-only` | Rebuild `report.csv` from the existing manifests. |
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

1. **Try 3 sites first:** `python collect.py --limit 3`, then look at `report.csv` and
   `sites/*/manifest.json`.
2. **Text is all there:** open a few random `index.html` files in a browser. Styling may look
   broken offline, which is fine. The text should all be there.
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

The tests serve a realistic mock Elementor site locally, plus maintenance, password-protected,
redirecting, JavaScript-rendered and unresolvable sites. They then check the manifest, the image
files and the report.
