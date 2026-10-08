import { Copy, Bold, Italic, Code, Heading, Heading1, Heading2, Heading3, Heading4, Heading5, Heading6, Pilcrow, List, ListOrdered, Table, Minus, Ellipsis, Save, BookOpen, Combine, Link, ThumbsUp, GitCompareArrows, Replace, ArrowDown, ArrowUpRight, Check, CalendarDays, ChevronDown, Circle, CircleSlash, RotateCcw, Square, Diamond, Library, Mic, Moon, Plus, Search, Send, Settings, Sparkle, Sun, Triangle, X, EqualApproximately, UserRound } from "lucide-react";

const icons = { copy: Copy, bold: Bold, italic: Italic, code: Code, link: Link, heading: Heading, 'heading-1': Heading1, 'heading-2': Heading2, 'heading-3': Heading3, 'heading-4': Heading4, 'heading-5': Heading5, 'heading-6': Heading6, paragraph: Pilcrow, 'bullet-list': List, 'ordered-list': ListOrdered, table: Table, minus: Minus, more: Ellipsis, save: Save, book: BookOpen, merge: Combine, pattern: Sparkle, related: Link, supports: ThumbsUp, refutes: GitCompareArrows, supersedes: Replace, calendar: CalendarDays, library: Library, settings: Settings, moon: Moon, sun: Sun, "chevron-down": ChevronDown, plus: Plus, mic: Mic, send: Send, close: X, x: X, drop: X, "arrow-down": ArrowDown, down: ArrowDown, "arrow-up-right": ArrowUpRight, open: ArrowUpRight, check: Check, search: Search, remember: Diamond, inspiration: Diamond, ask: Diamond, do: Triangle };

export function Icon({ name, size = 20, className, ...props }) {
  if (name === "workspace" || name === "workbench") {
    return <span {...props} className={className} style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: size, height: size, fontSize: size, lineHeight: 1, color: "inherit", ...props.style }} aria-hidden="true" data-icon={name}>✦</span>;
  }
  const Glyph = ({ duplicate_of: EqualApproximately, may_supersede: ArrowUpRight, supplement: Plus,
    differs: GitCompareArrows, new: Diamond, person: UserRound, 'review-reask': RotateCcw, 'review-unused': CircleSlash, 'review-stop': Square })[name] ?? icons[name] ?? Circle;
  return <Glyph {...props} className={className} size={size} strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" fill={name === "ask" || name === "review-stop" ? "currentColor" : "none"} aria-hidden="true" focusable="false" data-icon={name} />;
}

export default Icon;
