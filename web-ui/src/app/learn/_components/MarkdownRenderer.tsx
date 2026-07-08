import ReactMarkdown, { type Components } from "react-markdown";
import Link from "next/link";
import { Callout, type CalloutKind } from "./Callout";
import { LessonImage } from "./LessonImage";

const KINDS: CalloutKind[] = ["tip", "warning", "check", "do", "role", "note"];

const mdComponents: Components = {
  a({ href, children }) {
    return href && href.startsWith("/") ? (
      <Link href={href}>{children}</Link>
    ) : (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  img({ src, alt }) {
    return <LessonImage src={typeof src === "string" ? src : undefined} alt={alt} />;
  },
};

type Block =
  | { type: "md"; content: string }
  | { type: "callout"; kind: CalloutKind; title?: string; content: string };

/**
 * Split a lesson body into markdown + GitHub-style admonition blocks. An
 * admonition is a blockquote whose first line is `> [!KIND] optional title`,
 * e.g. `> [!DO] Approve the plan`. KIND ∈ tip|warning|check|do|role|note.
 */
function splitBlocks(md: string): Block[] {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let buf: string[] = [];
  const flush = () => {
    const c = buf.join("\n");
    if (c.trim()) blocks.push({ type: "md", content: c });
    buf = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^>\s*\[!(\w+)\]\s*(.*)$/i);
    const kind = m ? (m[1].toLowerCase() as CalloutKind) : null;
    if (m && kind && KINDS.includes(kind)) {
      flush();
      const inner: string[] = [];
      i++;
      while (i < lines.length && /^>/.test(lines[i])) {
        inner.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      i--; // step back to reprocess the non-quote line
      blocks.push({ type: "callout", kind, title: (m[2] || "").trim() || undefined, content: inner.join("\n") });
    } else {
      buf.push(lines[i]);
    }
  }
  flush();
  return blocks;
}

export function MarkdownRenderer({ body }: { body: string }) {
  const blocks = splitBlocks(body);
  return (
    <div className="prose prose-invert max-w-none prose-headings:font-semibold prose-a:text-primary prose-a:no-underline hover:prose-a:underline prose-code:rounded prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:text-sm prose-code:before:content-none prose-code:after:content-none prose-li:my-1">
      {blocks.map((b, i) =>
        b.type === "callout" ? (
          <Callout key={i} kind={b.kind} title={b.title}>
            <ReactMarkdown components={mdComponents}>{b.content}</ReactMarkdown>
          </Callout>
        ) : (
          <ReactMarkdown key={i} components={mdComponents}>
            {b.content}
          </ReactMarkdown>
        )
      )}
    </div>
  );
}
