import axe from "axe-core";

export async function expectNoSevereA11yViolations(container) {
  const result = await axe.run(container);
  const severe = result.violations.filter(
    (violation) => violation.impact === "critical" || violation.impact === "serious",
  );
  if (severe.length > 0) {
    const summary = severe.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      help: violation.help,
      targets: violation.nodes.map((node) => node.target),
    }));
    throw new Error(`Severe accessibility violations:\n${JSON.stringify(summary, null, 2)}`);
  }
}
