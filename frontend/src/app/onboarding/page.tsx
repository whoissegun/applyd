import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { isDevBypass } from "@/lib/devflags";
import { OnboardingWizard } from "./wizard";

export default async function OnboardingPage() {
  if (!isDevBypass()) {
    const supabase = await createClient();
    const {
      data: { user },
    } = await supabase.auth.getUser();
    if (!user) redirect("/login?next=/onboarding");
  }

  return (
    <main className="theme-eleven flex-1 flex flex-col">
      <header
        className="px-6 md:px-10 h-16 flex items-center justify-between"
        style={{ borderBottom: "1px solid #e5e5e5" }}
      >
        <Link
          href="/"
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
        <form action="/auth/logout" method="post">
          <button className="mono underline underline-offset-4" style={{ color: "var(--color-steel)" }}>
            Sign out
          </button>
        </form>
      </header>

      <section className="flex-1 flex justify-center px-6 py-10">
        <div className="w-full max-w-[680px]">
          <OnboardingWizard />
        </div>
      </section>
    </main>
  );
}
