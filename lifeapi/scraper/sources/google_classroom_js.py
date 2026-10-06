"""In-page extractors for Google Classroom.

Classroom's CSS class names are minified and change between releases, so these lean on
data-* attributes, ARIA labels, hrefs and text patterns instead.
"""

# Home page (/u/0/h): enrolled course cards.
COURSES_JS = r"""
() => [...document.querySelectorAll('li[data-course-id]')].map(li => {
  const link = li.querySelector('h2 a[href*="/c/"]');
  const lines = link ? link.innerText.split('\n').map(s => s.trim()).filter(Boolean) : [];
  const teacherBox = link && link.closest('h2') && link.closest('h2').nextElementSibling;
  return {
    id: li.dataset.courseId,
    href: link ? link.getAttribute('href') : null,
    name: lines[0] || null,
    section: lines[1] || null,
    teacher: teacherBox ? teacherBox.innerText.split('\n')[0].trim() || null : null,
  };
})
"""

# Classwork page (/u/0/w/<course>/t/all): one row per item. Call after expanding "View more".
CLASSWORK_JS = r"""
() => {
  const rows = [];
  const seen = new Set();
  for (const li of document.querySelectorAll('li[data-stream-item-id][data-stream-item-type]')) {
    if (seen.has(li.dataset.streamItemId)) continue;
    seen.add(li.dataset.streamItemId);
    const btn = li.querySelector('[role="button"][aria-label]');
    // Visible label text looks like "Assignment", "Completed Assignment", "Material", "Question"...
    const lines = li.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    const title = btn ? btn.getAttribute('aria-label') : null;
    const dateLine = lines.find(l => /^(Due|Posted|Edited|No due date)/.test(l)) || null;
    const topicEl = li.closest('[data-topic-id]');
    const topic = topicEl ? (topicEl.getAttribute('aria-label') || '').replace(/^Topic /, '') || null : null;
    const ti = lines.indexOf(title);
    const after = ti >= 0 ? lines.slice(ti + 1) : lines;
    const commentMatch = li.innerText.match(/(\d+) comments?/);
    // The grading category appears between the title/date and the trailing date column.
    const category = after.find(l => l !== dateLine && !/^(more_vert|More options|comment|\d+|\d+ comments?)$/.test(l) && !/^(Due|Posted|Edited)/.test(l)) || null;
    rows.push({
      id: li.dataset.streamItemId,
      type: li.dataset.streamItemType,
      label: lines[0] || null,
      title,
      date_text: dateLine,
      category,
      topic,
      comment_count: commentMatch ? +commentMatch[1] : 0,
    });
  }
  return rows;
}
"""

# Item detail page (/u/0/c/<course>/<a|m|sa|mc>/<item>/details).
DETAIL_JS = r"""
() => {
  // After an in-app redirect (/a/ -> /mc/) the previous view stays in the DOM, hidden;
  // always read the visible one.
  const visible = el => el.checkVisibility ? el.checkVisibility() : !!el.offsetParent;
  const header = [...document.querySelectorAll('[data-stream-item-id]')].find(visible);
  if (!header) return null;
  const main = header.closest('[role="main"]') || header.parentElement.parentElement;
  const h1 = header.querySelector('h1');
  const title = h1 ? h1.innerText.trim() : document.title.replace(/ - Classroom$/, '');
  const hlines = header.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const ti = hlines.indexOf(title);
  const meta = ti >= 0 ? hlines.slice(ti + 1) : hlines;

  const dotIdx = meta.indexOf('•');
  const author = dotIdx > 0 ? meta[dotIdx - 1] : null;
  const posted = dotIdx >= 0 ? meta[dotIdx + 1] : null;
  const htext = header.innerText;
  const graded = htext.match(/([\d.]+) points out of possible ([\d.]+)/);
  const points = htext.match(/(?:^|\n)([\d.]+) points?(?:\n|$)/);
  const due = hlines.find(l => /^Due /.test(l) || l === 'No due date') || null;
  // Category: the line right before the "•" that precedes the points, if any.
  let category = null;
  const secondDot = meta.indexOf('•', dotIdx + 1);
  if (secondDot > 0) category = meta[secondDot - 1];

  // Description: the first block of text in the main column after the header that isn't
  // an attachment, the "Your work" panel, a comment section or a button.
  const SKIP = '[data-material-parent-id], [data-submission-id], [data-type][data-visibility], [data-title-style], [role="toolbar"], [role="complementary"], button, [aria-hidden="true"]';
  const skip = el => {  // only consider ancestors inside `main`; page-level wrappers match too
    for (; el && el !== main; el = el.parentElement) if (el.matches(SKIP)) return true;
    return false;
  };
  let description = null, descBlock = null;
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const el = node.parentElement;
    if (!node.textContent.trim() || header.contains(el) || skip(el)) continue;
    let block = el;
    while (block.parentElement && block.parentElement !== main && !block.parentElement.contains(header)) block = block.parentElement;
    description = block.innerText.trim() || null;
    if (description) { descBlock = block; break; }  // hidden blocks have no innerText; keep looking
  }

  // Attachments live in material blocks: data-filter="0" holds what the teacher attached,
  // data-filter="1" holds the student's own work. (Don't use data-submission-id: the whole
  // page is wrapped in an element that carries it.) Wide layouts render blocks twice.
  const parseAtt = a => {
    const label = a.getAttribute('aria-label').replace(/^Attachment:\s*/, '');
    const m = /^Link to /.test(label) ? ['', 'Link', label.slice(8)] : label.match(/^([^:]+):\s*(.*)$/);  // "PDF: x.pdf", "Link to https://..."
    return { title: m ? m[2] : label, type: m ? m[1] : null, url: a.href };
  };
  const attsIn = filter => [...new Map([...document.querySelectorAll('a[aria-label^="Attachment"]')]
    .filter(a => { const b = a.closest('[data-material-parent-id]'); return b && b.dataset.filter === filter && visible(a); })
    .map(a => [a.href, parseAtt(a)])).values()];
  const attachments = attsIn('0');
  const submitted = attsIn('1');
  const links = descBlock ? [...new Set([...descBlock.querySelectorAll('a[href]')].map(a => a.href).filter(h => /^https?:/.test(h)))] : [];

  const statusEl = [...document.querySelectorAll('span[data-submission-id]')].find(visible);
  const status = statusEl ? (statusEl.innerText.split('\n').map(s => s.trim())
    .map(l => l.replace(/Estigfend/gi, '').trim())  // icon-font glyph text
    .find(l => l && !/loading/i.test(l)) || null) : null;

  const comments = [];
  const seenComments = new Set();
  for (const c of document.querySelectorAll('[data-comment-id]')) {
    const cid = c.dataset.commentId;
    if (seenComments.has(cid)) continue;  // wide layouts render comments twice
    // [data-comment-id] sits inside the author line; climb until the box also holds the text.
    let box = c, who = null, rest = [];
    while ((box = box.parentElement) && box !== document.body) {
      who = box.querySelector('a[aria-label^="Comment posted by"]');
      if (!who) continue;
      const lines = box.innerText.split('\n').map(s => s.trim()).filter(Boolean)
        .filter(l => !/^(Reply to this comment|more_vert|More options|•)$/.test(l));
      const name = who.innerText.trim();
      const ai = lines.findIndex(l => l.startsWith(name));
      rest = lines.slice(ai + 1);
      // Name and date are sometimes rendered on one line ("NAME • Sep 18").
      if (ai >= 0 && lines[ai] !== name) rest.unshift(lines[ai].slice(name.length));
      if (rest.length > 1) break;
    }
    if (!who) continue;
    seenComments.add(cid);
    comments.push({
      id: cid,
      author: who.innerText.trim(),
      posted_at: (rest[0] || '').replace(/^\s*•\s*/, '') || null,
      text: rest.slice(1).join('\n'),
      private: !!c.closest('[data-type="3"][data-visibility="1"]'),
    });
  }
  return {
    url: location.href, title, author, posted, due, category,
    points_possible: graded ? +graded[2] : (points ? +points[1] : null),
    score: graded ? +graded[1] : null,
    description, links, attachments, submitted, status, comments,
  };
}
"""

# Course stream (/u/0/c/<course>): announcements ("Post by ...").
STREAM_JS = r"""
() => {
  const posts = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[data-stream-item-id][data-include-stream-item-materials="false"]')) {
    const id = el.dataset.streamItemId;
    if (seen.has(id)) continue;
    const lines = el.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length || !/^Post by /.test(lines[0])) continue;  // skip "new assignment" stubs
    seen.add(id);
    const author = lines[0].replace(/^Post by /, '');
    const created = lines.find(l => /^Created /.test(l));
    const edited = lines.find(l => /^\(Edited /.test(l));
    // Body: the post's text minus its attachments and comment section, after the "More options" label.
    let text = el.innerText;
    for (const ex of el.querySelectorAll('[data-type][data-visibility], [data-material-parent-id], [role="textbox"], [role="toolbar"]')) {
      const t = ex.innerText.trim();
      if (t) text = text.replace(t, '');
    }
    const tl = text.split('\n').map(s => s.trim()).filter(Boolean);
    const mo = tl.lastIndexOf('More options');
    let body = tl.slice(mo + 1).filter(l => !/^(No class comments|Add comment|Add class comment…?|Post|Reply to this comment|\d+ class comments?)$/.test(l));
    const links = [...el.querySelectorAll('a[href]')]
      .filter(a => !a.closest('[data-type][data-visibility], [data-material-parent-id]') && !a.matches('[aria-label^="Attachment"]') && /^https?:/.test(a.href) && !a.href.includes('classroom.google.com'))
      .map(a => a.href);
    const card = el.parentElement;
    const scope = card || el;
    const attachments = [...scope.querySelectorAll(`[data-stream-item-id="${id}"] a[aria-label^="Attachment"], a[aria-label^="Attachment"]`)]
      .filter(a => { const s = a.closest('[data-stream-item-id]'); return s && s.dataset.streamItemId === id; })
      .map(a => {
        const label = a.getAttribute('aria-label').replace(/^Attachment:\s*/, '');
        const m = /^Link to /.test(label) ? ['', 'Link', label.slice(8)] : label.match(/^([^:]+):\s*(.*)$/);  // "PDF: x.pdf", "Link to https://..."
        return { title: m ? m[2] : label, type: m ? m[1] : null, url: a.href };
      });
    const cbox = document.querySelector(`[data-type="2"][data-visibility="2"][data-stream-item-id="${id}"]`);
    const cm = cbox && cbox.innerText.match(/(\d+) class comments?/);
    posts.push({
      id, author,
      posted: created ? created.replace(/^Created /, '') : null,
      edited: edited ? edited.replace(/^\(Edited |\)$/g, '') : null,
      body: body.join('\n') || null,
      attachments: [...new Map(attachments.map(a => [a.url, a])).values()],
      comment_count: cm ? +cm[1] : 0,
      links: [...new Set(links)],
    });
  }
  return posts;
}
"""
