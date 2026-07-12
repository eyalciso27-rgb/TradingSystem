import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SourcesScreen } from "../../components/sources-screen";
import { SESSION_COOKIE, verifySessionToken } from "../../lib/auth";

export const dynamic = "force-dynamic";

export default async function SourcesPage() {
  const cookieStore = await cookies();
  const session = verifySessionToken(
    cookieStore.get(SESSION_COOKIE)?.value,
    Math.floor(Date.now() / 1000),
  );
  if (session === null) {
    redirect("/login");
  }
  return <SourcesScreen operator={session.operator} role={session.role} />;
}
