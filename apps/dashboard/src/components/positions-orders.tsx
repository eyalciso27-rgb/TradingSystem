import { PositionsScreen } from "./operations-screens";
import { operationsDataSource } from "../lib/operations";
export async function PositionsOrders({ operator, role }: { operator: string; role: string }) { return <PositionsScreen operator={operator} role={role} snapshot={await operationsDataSource.getSnapshot()} />; }
