import { ExecutionHistoryScreen } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function ExecutionHistory({ operator, role }: { operator: string; role: string }) { return <ExecutionHistoryScreen operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
