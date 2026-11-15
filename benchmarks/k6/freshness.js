import { constantVus, freshnessProbe, systemTags, traffic, vus } from './common.js';

const scenarios = { probe: constantVus(1, 'runProbe') };
if (vus > 1) scenarios.background = constantVus(vus - 1, 'runBackground');

export const options = { systemTags, scenarios };

export function runProbe() {
  freshnessProbe();
}

export function runBackground() {
  traffic(false);
}
