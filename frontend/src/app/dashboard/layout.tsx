import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { isDevBypass, MOCK_USER, MOCK_PROFILE } from "@/lib/devflags";
import { SideNav } from "./_components/side-nav";

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  let user: { id: string; email: string };
  let needsOnboarding: boolean;

  if (isDevBypass()) {
    user = { id: MOCK_USER.id, email: MOCK_USER.email };
    needsOnboarding =
      !MOCK_PROFILE.full_name || !MOCK_PROFILE.phone || !MOCK_PROFILE.linkedin_url;
  } else {
    const supabase = await createClient();
    const {
      data: { user: authUser },
    } = await supabase.auth.getUser();
    if (!authUser) redirect("/login?next=/dashboard");
    user = { id: authUser.id, email: authUser.email ?? "" };

    const { data: profile } = await supabase
      .from("user_profiles")
      .select("full_name, phone, linkedin_url")
      .eq("id", authUser.id)
      .maybeSingle();

    needsOnboarding =
      !profile?.full_name || !profile?.phone || !profile?.linkedin_url;
  }

  return (
    <div className="theme-eleven flex flex-1 min-h-0">
      <aside
        className="hidden md:flex flex-col w-[220px] shrink-0 px-4 py-5 gap-6"
        style={{ borderRight: "1px solid var(--color-hairline)" }}
      >
        <Link
          href="/dashboard"
          className="px-2"
          style={{
            fontFamily: "var(--font-cormorant)",
            fontWeight: 400,
            fontSize: 22,
            letterSpacing: "-0.01em",
            color: "#000",
          }}
        >
          applyd
        </Link>
        <SideNav />
        <div className="mt-auto flex flex-col gap-3">
          {needsOnboarding ? (
            <Link href="/onboarding" className="pill pill-ghost pill-sm w-fit">
              Finish setup →
            </Link>
          ) : null}
          <div className="px-2 mono" style={{ fontSize: 11, color: "var(--color-steel)" }}>
            <div style={{ color: "var(--color-pebble)" }}>{user.email}</div>
            <form action="/auth/logout" method="post" className="mt-2">
              <button type="submit" className="underline underline-offset-4" style={{ color: "var(--color-steel)" }}>
                Sign out
              </button>
            </form>
          </div>
        </div>
      </aside>

      <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">{children}</main>
    </div>
  );
}
