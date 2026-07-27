import { describe, expect, it } from "vitest";

import { formatProbabilityInput } from "./policyModel";

describe("formatProbabilityInput", () => {
  it("removes float32 artifacts without padding meaningful decimals", () => {
    expect(formatProbabilityInput(0.15000000596046448)).toBe("0.15");
    expect(formatProbabilityInput(0.05000000074505806)).toBe("0.05");
    expect(formatProbabilityInput(0.699999988079071)).toBe("0.7");
    expect(formatProbabilityInput(0.123456)).toBe("0.123456");
  });
});
