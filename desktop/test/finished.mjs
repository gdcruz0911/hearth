// Preloaded into each test file's process by run-all.mjs: records that the file ran to its end, which a process.exit does not.

import { appendFileSync } from "node:fs";
import { after } from "node:test";

after(() => appendFileSync(process.env.HEARTH_TEST_FINISHED, `${process.argv[1]}\n`));
