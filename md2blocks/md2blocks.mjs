// md → Notion blocks for page.archive: markdown on stdin (page base URL as
// argv[2]), block JSON array on stdout. Uses @tryfabric/martian (installed
// alongside this file in the image). notionLimits.truncate clamps hard API
// limits (100 rich_text items etc.); long paragraphs are SPLIT into
// <=2000-char chunks, not truncated (verified).
import { markdownToBlocks } from '@tryfabric/martian';

const base = process.argv[2] || null;

// Notion rejects non-absolute link URLs (e.g. Turndown keeps hrefs raw, so
// "#fragment" and "/relative" survive extraction). Resolve against the page
// base; if that fails or isn't http(s), drop the link and keep the text.
const fixLinks = (node) => {
  if (Array.isArray(node)) { node.forEach(fixLinks); return; }
  if (!node || typeof node !== 'object') return;
  const link = node.text && node.text.link;
  if (link && link.url !== undefined) {
    let ok = false;
    try {
      const u = new URL(link.url, base || undefined);
      if (u.protocol === 'http:' || u.protocol === 'https:') { link.url = u.href; ok = true; }
    } catch (e) {}
    if (!ok) delete node.text.link;
  }
  Object.values(node).forEach(fixLinks);
};

let md = '';
process.stdin.on('data', (c) => (md += c));
process.stdin.on('end', () => {
  const blocks = markdownToBlocks(md, {
    notionLimits: { truncate: true },
    strictImageUrls: true,
  });
  fixLinks(blocks);
  process.stdout.write(JSON.stringify(blocks));
});
