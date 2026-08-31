import { DecisionsScreen } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function CandidateWorkspace({ operator, role }: { operator: string; role: string }) { return <DecisionsScreen operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
