import { updateSession } from "@/lib/supabase/middleware";
import type { NextRequest } from "next/server";

// Next 16 renamed `middleware` → `proxy`. Same contract; runs before the
// route renders. We use it to refresh Supabase sessions and gate private routes.
export async function proxy(request: NextRequest) {
  return updateSession(request);
}

export const config = {
  matcher: [
    // Skip Next internals and static files.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
