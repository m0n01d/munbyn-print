/*
 * rastertotspl -- native CUPS raster -> TSPL filter for the Munbyn RW403B.
 *
 * Replaces Munbyn's x86_64-only /Library/Printers/Munbyn/rastertorw403b so the
 * normal macOS Print dialog works on Apple Silicon without Rosetta.
 *
 * It emits ONLY the TSPL subset verified on this printer's firmware
 * (hardware test, 2026-09-27):
 *
 *   SIZE, GAP / BLINE, REFERENCE, OFFSET, SETC AUTODOTTED OFF, DENSITY,
 *   SPEED, DIRECTION, CLS, BITMAP (mode 1, raw), PRINT
 *
 * A job that also contained TEXT/BOX/BAR printed nothing at all, so never
 * add any other command here. The verified job for a 4x6in gap label at the
 * PPD defaults (every line ends in CR LF):
 *
 *   SIZE 102 mm,155 mm
 *   GAP 3 mm,0 mm
 *   REFERENCE 0,0
 *   OFFSET 0 mm
 *   SETC AUTODOTTED OFF
 *   DENSITY 12
 *   SPEED 4
 *   DIRECTION 0,0
 *   CLS
 *   BITMAP 0,0,102,1242,1,<126684 bytes>
 *   PRINT 1,1
 *
 * Feed correction (hardware-measured 2026-09-27): across the head the printer
 * is exact (8 dots/mm), but along the feed an 800-row bar prints 98.1 mm
 * long (feed scale 0.981, mechanical). So the PPD asks cgpdftoraster for
 * HWResolution [203 207] (207 = round(203 / 0.981)): the page is rendered
 * with 207 rows per inch, which this printer lays down as ~1 inch of paper.
 * A 4x6 page is then 812 x 1242, and SIZE is the bitmap size in printer
 * steps (1/203 in): 812 -> 102 mm, 1242 -> 155 mm -- the exact job above,
 * which printed a 100 mm test bar at ~100 mm. The uncorrected 203x203dpi
 * choice gives the older verified job, SIZE 102 mm,152 mm with 1218 rows.
 *
 * BITMAP data: rows top to bottom, MSB = leftmost dot, a CLEAR bit (0) prints
 * a BLACK dot, a set bit (1) is white; row padding bits are white.
 *
 * Usage (how CUPS runs it):
 *   rastertotspl job-id user title copies options [raster-file]
 *
 * Options (argv[5], falling back to the PPD named by $PPD, then built-ins):
 *   MediaType   0 continuous | 1 gap (default) | 2 black line
 *   GapHeight   gap / black-line height in mm (default 3)
 *   GapOffset   gap / black-line offset in mm (default 0)
 *   Darkness    1..16, emitted as DENSITY clamped to 0..15 (default 12)
 *   PrintSpeed  10..80 (PPD) or 1..8, emitted as SPEED 1..8 (default 40 -> 4)
 *   Horizontal  image shift in mm, + = right, negative crops (default 0);
 *               converted with the horizontal resolution
 *   Vertical    image shift in mm, + = down, negative crops (default 0);
 *               converted with the vertical (feed) resolution
 *   Rotate      vendor codes 0=0, 2=90, 1=180, 3=270 degrees clockwise (or
 *               90/180/270). Done in the bitmap: DIRECTION stays 0,0, the only
 *               verified value. 90/270 on a non-square raster (203x207) is
 *               resampled so the page keeps its physical aspect ratio.
 *   PrintMode   5 threshold (default) | 4 error diffusion (Floyd-Steinberg) |
 *               2 dispersed ordered dither | 3 clustered ordered dither
 *   Threshold   1..255 on 0-255 luminance; darker than this prints black in
 *               PrintMode 5 (default 160)
 *
 * Copies: the raster header's NumCopies (cgpdftoraster sets it to 1 when it
 * already produced collated copies itself, else to the copy count), falling
 * back to argv[4] when the header says 0.
 */

#include <cups/cups.h>
#include <cups/ppd.h>
#include <cups/raster.h>

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define NATIVE_DPI 203        /* the head: 8 dots/mm, exact across the head */
#define MAX_DPI 2400          /* sanity bound on HWResolution */
#define MAX_WIDTH_MM 108      /* vendor PPD MaxMediaWidth 306.14pt = 4.25in */
#define MAX_WIDTH_DOTS 864    /* 108 bytes per row */
#define MAX_HEIGHT_DOTS 40000 /* ~197in; a sanity bound, not a media limit */
#define MAX_COPIES 9999

enum { MEDIA_CONTINUOUS = 0, MEDIA_GAP = 1, MEDIA_BLINE = 2 };
enum {
  MODE_DIFFUSION = 2,       /* dispersed-dot ordered dither (8x8 Bayer) */
  MODE_GATHERING = 3,       /* clustered-dot ordered dither */
  MODE_ERROR_DIFFUSION = 4, /* Floyd-Steinberg */
  MODE_THRESHOLD = 5        /* vendor "Default": plain threshold */
};

typedef struct {
  int media;
  int gap_mm;
  int gap_offset_mm;
  int density;
  int speed;
  int rotate; /* degrees clockwise: 0, 90, 180, 270 */
  int h_offset_mm;
  int v_offset_mm;
  int print_mode;
  int threshold;
} settings_t;

static volatile sig_atomic_t canceled = 0;

static void on_sigterm(int sig) {
  (void)sig;
  canceled = 1;
}

/* rint() rounds half to even in the default FP mode, like Python's round(). */

/* Raster dots/rows -> whole mm at `res` dots per inch (SIZE). */
static int mm_from_dots(unsigned dots, unsigned res) {
  return (int)rint((double)dots * 25.4 / res);
}
/* mm -> dots/rows at `res` dots per inch (the offsets). */
static int dots_from_mm(double mm, unsigned res) {
  return (int)rint(mm / 25.4 * res);
}

/* ---------------------------------------------------------------------- */
/* Options                                                                */
/* ---------------------------------------------------------------------- */

typedef struct {
  int num_options;
  cups_option_t *options;
  ppd_file_t *ppd;
} optsrc_t;

static const char *option_value(const optsrc_t *src, const char *name) {
  const char *v = cupsGetOption(name, src->num_options, src->options);
  if (v && *v)
    return v;
  if (src->ppd) {
    ppd_choice_t *c = ppdFindMarkedChoice(src->ppd, name);
    if (c && c->choice[0])
      return c->choice;
  }
  return NULL;
}

static int int_option(const optsrc_t *src, const char *name, int def, int lo,
                      int hi) {
  const char *v = option_value(src, name);
  char *end = NULL;
  long n;

  if (!v)
    return def;
  errno = 0;
  n = strtol(v, &end, 10);
  if (end == v || *end != '\0' || errno) {
    fprintf(stderr, "DEBUG: ignoring %s=\"%s\" (not an integer), using %d\n",
            name, v, def);
    return def;
  }
  if (n < lo || n > hi) {
    long c = n < lo ? lo : hi;
    fprintf(stderr, "DEBUG: %s=%ld out of range %d..%d, clamped to %ld\n", name,
            n, lo, hi, c);
    n = c;
  }
  return (int)n;
}

static void read_settings(const optsrc_t *src, settings_t *s) {
  int darkness, raw_speed, rot, mode;

  s->media = int_option(src, "MediaType", MEDIA_GAP, 0, 2);
  s->gap_mm = int_option(src, "GapHeight", 3, 0, 30);
  s->gap_offset_mm = int_option(src, "GapOffset", 0, 0, 30);

  /* Vendor PPD offers Darkness 1..16; TSPL DENSITY is 0..15. Identity map so
   * the PPD default 12 emits the verified DENSITY 12; 16 clamps to 15. */
  darkness = int_option(src, "Darkness", 12, 0, 16);
  s->density = darkness > 15 ? 15 : darkness;

  /* Vendor PPD choices are 10..80 meaning speed 1..8; accept 1..8 as-is. */
  raw_speed = int_option(src, "PrintSpeed", 40, 1, 80);
  s->speed = raw_speed <= 8 ? raw_speed : (raw_speed + 5) / 10;
  if (s->speed < 1)
    s->speed = 1;
  if (s->speed > 8)
    s->speed = 8;

  s->h_offset_mm = int_option(src, "Horizontal", 0, -50, 50);
  s->v_offset_mm = int_option(src, "Vertical", 0, -50, 50);

  rot = int_option(src, "Rotate", 0, 0, 270);
  switch (rot) {
  case 0: s->rotate = 0; break;
  case 1: s->rotate = 180; break; /* vendor PPD: "Rotate 1/180" */
  case 2: s->rotate = 90; break;  /* vendor PPD: "Rotate 2/90" */
  case 3: s->rotate = 270; break; /* vendor PPD: "Rotate 3/270" */
  case 90:
  case 180:
  case 270: s->rotate = rot; break;
  default:
    fprintf(stderr, "DEBUG: ignoring Rotate=%d, using 0\n", rot);
    s->rotate = 0;
  }

  mode = int_option(src, "PrintMode", MODE_THRESHOLD, 0, 9);
  if (mode != MODE_DIFFUSION && mode != MODE_GATHERING &&
      mode != MODE_ERROR_DIFFUSION)
    mode = MODE_THRESHOLD;
  s->print_mode = mode;
  s->threshold = int_option(src, "Threshold", 160, 1, 255);
}

/* ---------------------------------------------------------------------- */
/* Raster line -> 8-bit luminance (0 = black, 255 = white)                */
/* ---------------------------------------------------------------------- */

static int is_gray_luminance(cups_cspace_t cs) {
  return cs == CUPS_CSPACE_W || cs == CUPS_CSPACE_SW;
}

static int is_rgb(cups_cspace_t cs) {
  return cs == CUPS_CSPACE_RGB || cs == CUPS_CSPACE_SRGB ||
         cs == CUPS_CSPACE_ADOBERGB;
}

/* Returns NULL when supported, else a reason. */
static const char *unsupported(const cups_page_header2_t *h) {
  unsigned bpc = h->cupsBitsPerColor;

  if (is_gray_luminance(h->cupsColorSpace) || h->cupsColorSpace == CUPS_CSPACE_K) {
    if (bpc != 1 && bpc != 2 && bpc != 4 && bpc != 8 && bpc != 16)
      return "gray bits per color must be 1, 2, 4, 8 or 16";
    if (h->cupsBitsPerPixel != bpc)
      return "gray raster must have one color per pixel";
    return NULL;
  }
  if (is_rgb(h->cupsColorSpace)) {
    if (h->cupsColorOrder != CUPS_ORDER_CHUNKED)
      return "RGB raster must be chunky (CUPS_ORDER_CHUNKED)";
    if (!((bpc == 8 && h->cupsBitsPerPixel == 24) ||
          (bpc == 16 && h->cupsBitsPerPixel == 48)))
      return "RGB raster must be 8 or 16 bits per color";
    return NULL;
  }
  return "color space must be W, SW, K, RGB, sRGB or AdobeRGB";
}

static void line_to_luma(const cups_page_header2_t *h, const unsigned char *in,
                         unsigned char *out, unsigned width) {
  unsigned bpc = h->cupsBitsPerColor;
  unsigned x;

  if (is_rgb(h->cupsColorSpace)) {
    for (x = 0; x < width; x++) {
      unsigned r, g, b;
      if (bpc == 8) {
        r = in[3 * x];
        g = in[3 * x + 1];
        b = in[3 * x + 2];
      } else {
        uint16_t v[3];
        memcpy(v, in + 6 * x, sizeof(v)); /* host order after ReadPixels */
        r = v[0] >> 8;
        g = v[1] >> 8;
        b = v[2] >> 8;
      }
      out[x] = (unsigned char)((r * 77 + g * 150 + b * 29) >> 8);
    }
    return;
  }

  for (x = 0; x < width; x++) {
    unsigned v;
    switch (bpc) {
    case 1: v = ((in[x >> 3] >> (7 - (x & 7))) & 1) ? 255 : 0; break;
    case 2: v = ((in[x >> 2] >> (6 - 2 * (x & 3))) & 3) * 85; break;
    case 4: v = ((in[x >> 1] >> ((x & 1) ? 0 : 4)) & 15) * 17; break;
    case 8: v = in[x]; break;
    default: {
      uint16_t s;
      memcpy(&s, in + 2 * x, sizeof(s));
      v = s >> 8;
    }
    }
    /* W/SW are luminance (0 = black); K is ink (max = black). */
    out[x] = (unsigned char)(h->cupsColorSpace == CUPS_CSPACE_K ? 255 - v : v);
  }
}

/* ---------------------------------------------------------------------- */
/* Rotation, shift and binarization                                       */
/* ---------------------------------------------------------------------- */

/* Pixel of the page rotated clockwise by `rot` degrees, at (x, y) of the
 * rotated image. */
static unsigned char rotated_pixel(const unsigned char *g, unsigned pw,
                                   unsigned ph, int rot, unsigned x,
                                   unsigned y) {
  switch (rot) {
  case 90: return g[(size_t)(ph - 1 - x) * pw + y];
  case 180: return g[(size_t)(ph - 1 - y) * pw + (pw - 1 - x)];
  case 270: return g[(size_t)x * pw + (pw - 1 - y)];
  default: return g[(size_t)y * pw + x];
  }
}

/*
 * The rotated page (rw x rh source pixels) drawn as tw x th label dots. Equal
 * sizes (always, except 90/270 on a non-square-dpi raster) copy the pixel
 * exactly; otherwise it is a bilinear resample of the gray image, before any
 * thresholding, so the rotated page keeps its physical aspect ratio.
 */
static unsigned char sample_pixel(const unsigned char *g, unsigned pw,
                                  unsigned ph, int rot, unsigned rw,
                                  unsigned rh, unsigned tw, unsigned th,
                                  unsigned x, unsigned y) {
  double fx, fy, ax, ay, top, bot;
  unsigned x0, y0, x1, y1;

  if (tw == rw && th == rh)
    return rotated_pixel(g, pw, ph, rot, x, y);

  fx = ((double)x + 0.5) * rw / tw - 0.5;
  fy = ((double)y + 0.5) * rh / th - 0.5;
  if (fx < 0)
    fx = 0;
  if (fy < 0)
    fy = 0;
  x0 = (unsigned)fx;
  y0 = (unsigned)fy;
  if (x0 >= rw - 1) {
    x0 = x1 = rw - 1;
    ax = 0;
  } else {
    x1 = x0 + 1;
    ax = fx - x0;
  }
  if (y0 >= rh - 1) {
    y0 = y1 = rh - 1;
    ay = 0;
  } else {
    y1 = y0 + 1;
    ay = fy - y0;
  }
  top = (1 - ax) * rotated_pixel(g, pw, ph, rot, x0, y0) +
        ax * rotated_pixel(g, pw, ph, rot, x1, y0);
  bot = (1 - ax) * rotated_pixel(g, pw, ph, rot, x0, y1) +
        ax * rotated_pixel(g, pw, ph, rot, x1, y1);
  return (unsigned char)((1 - ay) * top + ay * bot + 0.5);
}

static const unsigned char BAYER8[8][8] = {
    {0, 32, 8, 40, 2, 34, 10, 42},  {48, 16, 56, 24, 50, 18, 58, 26},
    {12, 44, 4, 36, 14, 46, 6, 38}, {60, 28, 52, 20, 62, 30, 54, 22},
    {3, 35, 11, 43, 1, 33, 9, 41},  {51, 19, 59, 27, 49, 17, 57, 25},
    {15, 47, 7, 39, 13, 45, 5, 37}, {63, 31, 55, 23, 61, 29, 53, 21}};

static const unsigned char CLUSTER4[4][4] = {
    {12, 5, 6, 13}, {4, 0, 1, 7}, {11, 3, 2, 8}, {15, 10, 9, 14}};

/*
 * Build the label's packed BITMAP payload from the page's luminance image.
 * Label is lw x lh dots; content is the rotated page (rw x rh source pixels,
 * drawn as tw x th dots) shifted by (dx, dy) dots, cropped at the label
 * edges, white elsewhere.
 */
static void render_label(const unsigned char *gray, unsigned pw, unsigned ph,
                         const settings_t *s, unsigned rw, unsigned rh,
                         unsigned tw, unsigned th, unsigned lw, unsigned lh,
                         int dx, int dy, unsigned char *bits, unsigned wb,
                         unsigned char *lumrow, int *err_cur, int *err_next) {
  unsigned x, y;

  memset(bits, 0xFF, (size_t)wb * lh); /* all white, incl. row padding */
  if (s->print_mode == MODE_ERROR_DIFFUSION)
    memset(err_cur, 0, sizeof(int) * (lw + 2));

  for (y = 0; y < lh; y++) {
    unsigned char *row = bits + (size_t)y * wb;
    long sy = (long)y - dy;

    for (x = 0; x < lw; x++) {
      long sx = (long)x - dx;
      if (sy < 0 || sx < 0 || (unsigned long)sy >= th || (unsigned long)sx >= tw)
        lumrow[x] = 255;
      else
        lumrow[x] = sample_pixel(gray, pw, ph, s->rotate, rw, rh, tw, th,
                                 (unsigned)sx, (unsigned)sy);
    }

    if (s->print_mode == MODE_ERROR_DIFFUSION) {
      int *tmp;
      memset(err_next, 0, sizeof(int) * (lw + 2));
      for (x = 0; x < lw; x++) {
        int v = lumrow[x] + err_cur[x + 1] / 16;
        int black = v < 128;
        int e = v - (black ? 0 : 255);
        if (black)
          row[x >> 3] &= (unsigned char)~(0x80u >> (x & 7));
        err_cur[x + 2] += e * 7;
        err_next[x] += e * 3;
        err_next[x + 1] += e * 5;
        err_next[x + 2] += e;
      }
      tmp = err_cur;
      err_cur = err_next;
      err_next = tmp;
      continue;
    }

    for (x = 0; x < lw; x++) {
      int t;
      switch (s->print_mode) {
      case MODE_DIFFUSION: t = BAYER8[y & 7][x & 7] * 4 + 2; break;
      case MODE_GATHERING: t = CLUSTER4[y & 3][x & 3] * 16 + 8; break;
      default: t = s->threshold;
      }
      if (lumrow[x] < t)
        row[x >> 3] &= (unsigned char)~(0x80u >> (x & 7));
    }
  }
}

/* ---------------------------------------------------------------------- */
/* Output                                                                 */
/* ---------------------------------------------------------------------- */

static int write_all(const void *buf, size_t len) {
  if (len && fwrite(buf, 1, len, stdout) != len) {
    fprintf(stderr, "ERROR: write to printer failed: %s\n", strerror(errno));
    return -1;
  }
  return 0;
}

static int format_header(char *out, size_t outlen, const settings_t *s,
                         int width_mm, int height_mm) {
  char media[64];

  if (s->media == MEDIA_CONTINUOUS)
    snprintf(media, sizeof(media), "GAP 0,0");
  else
    snprintf(media, sizeof(media), "%s %d mm,%d mm",
             s->media == MEDIA_BLINE ? "BLINE" : "GAP", s->gap_mm,
             s->gap_offset_mm);
  return snprintf(out, outlen,
                  "SIZE %d mm,%d mm\r\n"
                  "%s\r\n"
                  "REFERENCE 0,0\r\n"
                  "OFFSET 0 mm\r\n"
                  "SETC AUTODOTTED OFF\r\n"
                  "DENSITY %d\r\n"
                  "SPEED %d\r\n"
                  "DIRECTION 0,0\r\n",
                  width_mm, height_mm, media, s->density, s->speed);
}

static void usage(void) {
  fputs("Usage: rastertotspl job-id user title copies options [file]\n", stderr);
}

int main(int argc, char *argv[]) {
  int fd = 0;
  cups_raster_t *ras;
  cups_page_header2_t h;
  optsrc_t src = {0, NULL, NULL};
  settings_t s;
  struct sigaction action;
  unsigned page = 0;
  int argv_copies, status = 0;
  char prev_header[512] = "";
  unsigned char *line = NULL, *gray = NULL, *bits = NULL, *lumrow = NULL;
  int *err_a = NULL, *err_b = NULL;
  const char *ppd_path;

  if (argc < 6 || argc > 7) {
    usage();
    return 1;
  }

  memset(&action, 0, sizeof(action));
  sigemptyset(&action.sa_mask);
  action.sa_handler = on_sigterm;
  sigaction(SIGTERM, &action, NULL);

  argv_copies = atoi(argv[4]);
  if (argv_copies < 1)
    argv_copies = 1;

  src.num_options = cupsParseOptions(argv[5], 0, &src.options);
  ppd_path = getenv("PPD");
  if (ppd_path && *ppd_path) {
    src.ppd = ppdOpenFile(ppd_path);
    if (src.ppd) {
      ppdMarkDefaults(src.ppd);
      cupsMarkOptions(src.ppd, src.num_options, src.options);
    } else {
      fprintf(stderr, "DEBUG: could not open PPD %s; using built-in defaults\n",
              ppd_path);
    }
  }
  read_settings(&src, &s);
  fprintf(stderr,
          "DEBUG: rastertotspl: media=%d gap=%d offset=%d density=%d speed=%d "
          "rotate=%d shift=%d,%d mode=%d threshold=%d argv-copies=%d\n",
          s.media, s.gap_mm, s.gap_offset_mm, s.density, s.speed, s.rotate,
          s.h_offset_mm, s.v_offset_mm, s.print_mode, s.threshold, argv_copies);

  if (argc == 7) {
    fd = open(argv[6], O_RDONLY);
    if (fd < 0) {
      fprintf(stderr, "ERROR: unable to open raster file %s: %s\n", argv[6],
              strerror(errno));
      status = 1;
      goto done;
    }
  }
  ras = cupsRasterOpen(fd, CUPS_RASTER_READ);
  if (!ras) {
    fputs("ERROR: unable to read the CUPS raster stream\n", stderr);
    status = 1;
    goto done;
  }

  while (!canceled && cupsRasterReadHeader2(ras, &h)) {
    unsigned pw = h.cupsWidth, ph = h.cupsHeight, rw, rh, tw, th, lw, lh, wb, y;
    unsigned xres = h.HWResolution[0], yres = h.HWResolution[1];
    int width_mm, height_mm, dx, dy, copies, hlen;
    char hdr[512], cmd[128];
    const char *why;
    size_t nbits;

    page++;
    fprintf(stderr,
            "DEBUG: page %u: %ux%u px, %u bpc, %u bpp, cspace %d, "
            "%ux%u dpi, %gx%g pt, NumCopies %u\n",
            page, pw, ph, h.cupsBitsPerColor, h.cupsBitsPerPixel,
            (int)h.cupsColorSpace, h.HWResolution[0], h.HWResolution[1],
            h.cupsPageSize[0], h.cupsPageSize[1], h.NumCopies);

    if ((why = unsupported(&h)) != NULL) {
      fprintf(stderr, "ERROR: unsupported raster on page %u: %s\n", page, why);
      status = 1;
      break;
    }
    if (pw == 0 || ph == 0 || ph > MAX_HEIGHT_DOTS || pw > 4 * MAX_WIDTH_DOTS ||
        h.cupsBytesPerLine == 0 ||
        h.cupsBytesPerLine <
            ((size_t)pw * h.cupsBitsPerPixel + 7) / 8) {
      fprintf(stderr, "ERROR: page %u has unusable dimensions %ux%u "
                      "(cupsBytesPerLine %u too small for %u bpp)\n",
              page, pw, ph, h.cupsBytesPerLine, h.cupsBitsPerPixel);
      status = 1;
      break;
    }
    if (xres == 0 || yres == 0 || xres > MAX_DPI || yres > MAX_DPI) {
      fprintf(stderr, "ERROR: page %u has unusable resolution %ux%u dpi\n",
              page, xres, yres);
      status = 1;
      break;
    }
    if (xres != NATIVE_DPI)
      fprintf(stderr, "DEBUG: raster is %u dpi across the head, not %d; label "
                      "will be mis-scaled\n", xres, NATIVE_DPI);
    if (yres != xres)
      fprintf(stderr, "DEBUG: feed-corrected raster: %u rows per inch for a "
                      "%u dpi head (feed scale %.4f)\n",
              yres, xres, (double)xres / yres);

    free(line);
    free(gray);
    line = malloc(h.cupsBytesPerLine);
    gray = malloc((size_t)pw * ph);
    if (!line || !gray) {
      fputs("ERROR: out of memory\n", stderr);
      status = 1;
      break;
    }
    for (y = 0; y < ph; y++) {
      if (cupsRasterReadPixels(ras, line, h.cupsBytesPerLine) !=
          h.cupsBytesPerLine) {
        fprintf(stderr, "ERROR: short raster data on page %u, line %u\n", page,
                y);
        status = 1;
        break;
      }
      line_to_luma(&h, line, gray + (size_t)y * pw, pw);
    }
    if (status || canceled)
      break;

    /*
     * Label geometry comes from the raster, not from PageSize: columns are
     * head dots at xres, rows are feed steps rendered at yres (207 for the
     * feed-corrected 203x207 PPD choice). rw x rh is the rotated page in
     * source pixels; tw x th is that page in label dots/rows. They differ
     * only for 90/270 on a non-square raster, where the page's width (xres
     * pixels) becomes the feed axis (yres rows) and vice versa.
     */
    rw = tw = pw;
    rh = th = ph;
    if (s.rotate == 90 || s.rotate == 270) {
      rw = tw = ph;
      rh = th = pw;
      if (xres != yres) {
        double dtw = rint((double)ph * xres / yres);
        double dth = rint((double)pw * yres / xres);
        if (dtw < 1 || dth < 1 || dtw > 4 * MAX_WIDTH_DOTS ||
            dth > MAX_HEIGHT_DOTS) {
          fprintf(stderr, "ERROR: page %u: rotated page would be %.0fx%.0f "
                          "dots\n", page, dtw, dth);
          status = 1;
          break;
        }
        tw = (unsigned)dtw;
        th = (unsigned)dth;
      }
    }
    /* SIZE is the bitmap in printer steps (1/xres in, both axes): the
     * firmware measures the feed in the same nominal 8 dots/mm steps, so a
     * feed-corrected 4x6 (1242 rows) is "155 mm", as verified on paper. */
    width_mm = mm_from_dots(tw, xres);
    height_mm = mm_from_dots(th, xres);
    if (width_mm > MAX_WIDTH_MM) {
      /* Refuse rather than silently clamp+crop: for 90/270 this width is the
       * *rotated* page swapped into the SIZE line, and clamping it to
       * MAX_WIDTH_MM would emit a SIZE that matches no media the printer
       * could have loaded (found by review -- e.g. a portrait 4x6 label
       * rotated 270 degrees previously emitted "SIZE 108 mm,102 mm", which
       * is neither the loaded 102x152mm stock nor the rotated 152x102mm
       * page, with the excess silently cropped off). Only 0/180 keep SIZE
       * equal to the physical PageSize the user chose; a 90/270 rotation
       * whose swapped width doesn't fit that same media is a configuration
       * error, not something to paper over. */
      fprintf(stderr,
              "ERROR: page %u: label width %d mm > %d mm after a %d degree "
              "rotate -- SIZE would no longer match the loaded media\n",
              page, width_mm, MAX_WIDTH_MM, s.rotate);
      status = 1;
      break;
    }
    lw = tw > MAX_WIDTH_DOTS ? MAX_WIDTH_DOTS : tw;
    if (lw != tw)
      fprintf(stderr, "DEBUG: image %u dots wide > %u, right side cropped\n", tw,
              MAX_WIDTH_DOTS);
    lh = th;
    wb = (lw + 7) / 8;
    dx = dots_from_mm(s.h_offset_mm, xres);
    dy = dots_from_mm(s.v_offset_mm, yres); /* rows: the feed resolution */

    free(bits);
    free(lumrow);
    free(err_a);
    free(err_b);
    nbits = (size_t)wb * lh;
    bits = malloc(nbits);
    lumrow = malloc(lw);
    err_a = calloc(lw + 2, sizeof(int));
    err_b = calloc(lw + 2, sizeof(int));
    if (!bits || !lumrow || !err_a || !err_b) {
      fputs("ERROR: out of memory\n", stderr);
      status = 1;
      break;
    }
    render_label(gray, pw, ph, &s, rw, rh, tw, th, lw, lh, dx, dy, bits, wb,
                 lumrow, err_a, err_b);

    copies = h.NumCopies > 0 ? (int)h.NumCopies : argv_copies;
    if (copies > MAX_COPIES)
      copies = MAX_COPIES;

    /* Header once per job; again only if the label geometry changes. */
    hlen = format_header(hdr, sizeof(hdr), &s, width_mm, height_mm);
    if (strcmp(hdr, prev_header) != 0) {
      if (write_all(hdr, (size_t)hlen))
        goto write_failed;
      memcpy(prev_header, hdr, (size_t)hlen + 1);
    }
    snprintf(cmd, sizeof(cmd), "CLS\r\nBITMAP 0,0,%u,%u,1,", wb, lh);
    if (write_all(cmd, strlen(cmd)) || write_all(bits, nbits))
      goto write_failed;
    snprintf(cmd, sizeof(cmd), "\r\nPRINT 1,%d\r\n", copies);
    if (write_all(cmd, strlen(cmd)))
      goto write_failed;
    fflush(stdout);
    fprintf(stderr, "PAGE: %u %d\n", page, copies);
    fprintf(stderr,
            "DEBUG: page %u -> SIZE %d mm,%d mm, BITMAP %ux%u dots (%u bytes/row), "
            "shift %d,%d dots, PRINT 1,%d\n",
            page, width_mm, height_mm, lw, lh, wb, dx, dy, copies);
    continue;

  write_failed:
    status = 1;
    break;
  }

  cupsRasterClose(ras);
  if (!status && !canceled && page == 0) {
    fputs("ERROR: no pages found in the raster stream\n", stderr);
    status = 1;
  }
  if (canceled)
    fputs("DEBUG: job canceled\n", stderr);

done:
  if (fd > 0)
    close(fd);
  free(line);
  free(gray);
  free(bits);
  free(lumrow);
  free(err_a);
  free(err_b);
  if (src.ppd)
    ppdClose(src.ppd);
  cupsFreeOptions(src.num_options, src.options);
  return status;
}
