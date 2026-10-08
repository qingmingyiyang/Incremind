const WIDTH = 480, PAGE_HEIGHT = 720, PADDING = 24, SCALE = 2;
const bodyStyles = {
  label: { size: 12, height: 20, family: 'sans', weight: 500, color: 'muted' },
  question: { size: 15, height: 24, family: 'sans', weight: 400, color: 'ink2' },
  answer: { size: 18, height: 28, family: 'serif', weight: 700, color: 'ink' },
  title: { size: 14, height: 24, family: 'sans', weight: 500, color: 'ink2' },
  quote: { size: 13, height: 20, family: 'sans', weight: 400, color: 'ink2' },
};

function textRows(value, measure, style, key) {
  const lines = value.split(/(\r\n|\r|\n)/), rows = [];
  const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
  for (let index = 0; index < lines.length; index += 2) {
    let line = '';
    for (const { segment } of segmenter.segment(lines[index])) {
      if (measure(segment, style) > WIDTH - 2 * PADDING) throw new Error('图片文字过宽');
      if (line && measure(line + segment, style) > WIDTH - 2 * PADDING) {
        rows.push({ key, text: line, break: '', ...style }); line = '';
      }
      line += segment;
    }
    rows.push({ key, text: line, break: lines[index + 1] || '', ...style });
  }
  return rows;
}

// 每行保留原换行符，分页只改变排版位置，不裁切字符或引用片段。
export function layoutConversationImage(snapshot, measure) {
  const blocks = [
    { key: 'question-label', text: '问题', style: bodyStyles.label },
    { key: 'question', text: snapshot.question, style: bodyStyles.question },
    { key: 'answer-label', text: '回答', style: bodyStyles.label },
    { key: 'answer', text: snapshot.answer, style: bodyStyles.answer },
  ];
  if (snapshot.citations.length) blocks.push({ key: 'citations-label', text: '引用', style: bodyStyles.label });
  snapshot.citations.forEach((citation, index) => blocks.push(
    { key: `citation-${index}-title`, text: citation.title, style: bodyStyles.title },
    { key: `citation-${index}-quote`, text: citation.quote, style: bodyStyles.quote },
  ));
  const pages = [{ rows: [] }]; let y = 64;
  for (const block of blocks) {
    for (const row of textRows(block.text, measure, block.style, block.key)) {
      if (y + row.height > PAGE_HEIGHT - PADDING - 24) { pages.push({ rows: [] }); y = 64; }
      pages.at(-1).rows.push({ ...row, y }); y += row.height;
    }
    y += 8;
  }
  return { pages, width: WIDTH, pageHeight: PAGE_HEIGHT, padding: PADDING };
}

export function conversationImageTheme(element = document.documentElement) {
  const styles = getComputedStyle(element);
  return Object.fromEntries(['paper', 'ink', 'ink2', 'muted', 'line', 'serif', 'sans', 'mono']
    .map(name => [name, styles.getPropertyValue(`--${name}`).trim()]));
}
function validateTheme(theme) {
  if (['paper', 'ink', 'ink2', 'muted', 'line', 'serif', 'sans', 'mono'].some(name => typeof theme[name] !== 'string' || !theme[name].trim())) throw new Error('图片样式未读取');
}
const font = (style, theme) => `${style.weight} ${style.size}px ${theme[style.family]}`;
const encode = canvas => new Promise((resolve, reject) => canvas.toBlob(blob => blob?.type === 'image/png' ? resolve(blob) : reject(new Error('图片未生成')), 'image/png'));

// 只绘制快照文字及本机 token，不截图上下文面板，也不读取任何远程图像。
export async function createConversationImagePages(snapshot, { theme = conversationImageTheme(), createCanvas = () => document.createElement('canvas') } = {}) {
  validateTheme(theme);
  await document.fonts?.ready;
  const first = createCanvas(), measuring = first.getContext('2d');
  if (!measuring) throw new Error('图片绘制不可用');
  const layout = layoutConversationImage(snapshot, (value, style) => { measuring.font = font(style, theme); return measuring.measureText(value).width; });
  const pages = [];
  for (const [index, page] of layout.pages.entries()) {
    const canvas = index === 0 ? first : createCanvas();
    canvas.width = layout.width * SCALE; canvas.height = layout.pageHeight * SCALE;
    const context = canvas.getContext('2d'); if (!context) throw new Error('图片绘制不可用');
    context.scale(SCALE, SCALE); context.textBaseline = 'top';
    context.fillStyle = theme.paper; context.fillRect(0, 0, layout.width, layout.pageHeight);
    context.fillStyle = theme.ink; context.font = `700 22px ${theme.serif}`; context.fillText('对话', PADDING, PADDING);
    for (const row of page.rows) {
      context.font = font(row, theme); context.fillStyle = theme[row.color]; context.fillText(row.text, PADDING, row.y);
    }
    context.fillStyle = theme.line; context.fillRect(PADDING, PAGE_HEIGHT - PADDING - 20, WIDTH - 2 * PADDING, 1);
    context.fillStyle = theme.muted; context.font = `400 12px ${theme.mono}`; context.fillText(`${index + 1} / ${layout.pages.length}`, PADDING, PAGE_HEIGHT - PADDING);
    pages.push({ blob: await encode(canvas), filename: layout.pages.length === 1 ? '对话.png' : `对话-${index + 1}.png`, width: canvas.width, height: canvas.height });
  }
  return pages;
}
