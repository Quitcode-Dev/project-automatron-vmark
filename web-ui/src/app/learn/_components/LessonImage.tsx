/* eslint-disable @next/next/no-img-element */

/**
 * Captioned screenshot for lessons. Images live in /public/learn/*.png and are
 * referenced from markdown as `![caption](/learn/xxx.png)`. The alt text becomes
 * the caption. Uses a plain <img> (static assets; no next/image config needed).
 */
export function LessonImage({ src, alt }: { src?: string; alt?: string }) {
  if (!src) return null;
  return (
    <figure className="not-prose my-5">
      <img
        src={src}
        alt={alt ?? ""}
        loading="lazy"
        className="w-full rounded-lg border border-border bg-card shadow-sm"
      />
      {alt ? (
        <figcaption className="mt-2 text-center text-xs text-muted-foreground">
          {alt}
        </figcaption>
      ) : null}
    </figure>
  );
}
