import { useEffect, useRef } from "react";

// Each independently refreshable resource has its own sequence. Cleanup also
// invalidates mutations, including transports which ignore AbortSignal.
export function useRequestEpoch() {
  const epoch = useRef(0);
  const sequences = useRef(new Map<string, number>());
  useEffect(() => () => { epoch.current += 1; }, []);
  return (resource: string) => {
    const generation = epoch.current;
    const sequence = (sequences.current.get(resource) || 0) + 1;
    sequences.current.set(resource, sequence);
    return () => generation === epoch.current && sequences.current.get(resource) === sequence;
  };
}
