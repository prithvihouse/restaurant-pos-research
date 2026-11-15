import { duration, systemTags, traffic, workload } from './common.js';

const rate = Number(__ENV.POS_RATE);
const allocated = Number(__ENV.POS_OPEN_VUS);

export const options = {
  systemTags,
  scenarios: {
    arrival: {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration: String(duration) + 's',
      preAllocatedVUs: allocated,
      maxVUs: allocated,
      gracefulStop: '3s',
      exec: 'sendOne',
    },
  },
};

export function sendOne() {
  traffic(workload === 'read', false);
}
