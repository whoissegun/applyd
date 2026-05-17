"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/lib/supabase/server";

export async function saveProfile(formData: FormData) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return { error: "not signed in" };

  const payload = {
    full_name: String(formData.get("full_name") ?? "").trim(),
    phone: String(formData.get("phone") ?? "").trim() || null,
    linkedin_url: String(formData.get("linkedin_url") ?? "").trim() || null,
    github_url: String(formData.get("github_url") ?? "").trim() || null,
    portfolio_url: String(formData.get("portfolio_url") ?? "").trim() || null,
    profile_answers: String(formData.get("profile_answers") ?? "").trim() || null,
  };

  const { error } = await supabase
    .from("user_profiles")
    .update(payload)
    .eq("id", user.id);

  if (error) return { error: error.message };
  revalidatePath("/dashboard/profile");
  revalidatePath("/dashboard");
  return { ok: true as const };
}
