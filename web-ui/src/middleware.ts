import { auth } from "@/auth";
import {
  NextResponse,
  type NextRequest,
  type NextFetchEvent,
} from "next/server";

// Paths that should bypass auth. Order matters — first match wins.
const PUBLIC_PATHS = [
  "/login",
  "/api/auth", // Auth.js endpoints (callback, signin, csrf, etc.)
  "/_next",
  "/favicon.ico",
  "/notification.mp3",
];

function isPublic(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

// Local-dev escape hatch (mirrors the backend's AUTOMATRON_DEV_NO_AUTH): skip
// auth entirely so a test browser / headless run can reach the app without
// Google login. MUST stay unset in production.
const DEV_NO_AUTH = process.env.AUTOMATRON_DEV_NO_AUTH === "true";

const guarded = auth((req) => {
  const { pathname } = req.nextUrl;

  if (isPublic(pathname)) {
    return NextResponse.next();
  }

  if (!req.auth) {
    // For API requests, return 401 JSON. For UI requests, redirect to /login.
    if (pathname.startsWith("/api/")) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }
    const loginUrl = new URL("/login", req.nextUrl.origin);
    loginUrl.searchParams.set("callbackUrl", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
});

export default function middleware(req: NextRequest, event: NextFetchEvent) {
  if (DEV_NO_AUTH) {
    return NextResponse.next();
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (guarded as any)(req, event);
}

export const config = {
  // Run on every path EXCEPT static assets that don't go through Next.js.
  // /api/webhooks/* flows through Traefik straight to the orchestrator, NOT
  // through Next.js, so we don't need to exempt it here.
  matcher: ["/((?!_next/static|_next/image|.*\\..*).*)"],
};
