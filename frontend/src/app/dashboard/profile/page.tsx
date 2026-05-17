import { createClient } from "@/lib/supabase/server";
import { isDevBypass, MOCK_PROFILE } from "@/lib/devflags";
import { ProfileForm } from "./form";

export default async function ProfilePage() {
  let profile: typeof MOCK_PROFILE | null;
  if (isDevBypass()) {
    profile = MOCK_PROFILE;
  } else {
    const supabase = await createClient();
    const { data: { user } } = await supabase.auth.getUser();
    const { data } = await supabase
      .from("user_profiles")
      .select(
        "full_name, phone, linkedin_url, github_url, portfolio_url, profile_answers",
      )
      .eq("id", user!.id)
      .maybeSingle();
    profile = (data ?? null) as typeof MOCK_PROFILE | null;
  }

  return (
    <div className="px-8 py-8 max-w-[820px] mx-auto flex flex-col gap-8">
      <header>
        <p className="eyebrow">Settings</p>
        <h1 className="h-eleven mt-3">Your profile.</h1>
        <p className="mt-3" style={{ color: "#777169", fontSize: 15, lineHeight: 1.5 }}>
          These are the details we send with every application. Keep them up to date.
        </p>
      </header>
      <ProfileForm initial={profile ?? null} />
    </div>
  );
}
