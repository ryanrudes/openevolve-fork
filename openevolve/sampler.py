# Copyright 2023 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Class for sampling new programs."""

from collections.abc import Collection, Sequence

import llm
import numpy as np
import time
import logging
from openai import OpenAI

from openevolve import code_manipulation, evaluator
from openevolve import programs_database
from openevolve.structured_outputs import ProgramImplementation

import os
from pathlib import Path
from dotenv import load_dotenv

class LLM:
    """Language model that predicts continuation of provided source code."""

    def __init__(
        self, samples_per_prompt: int, model: llm.Model, log_path=None
    ) -> None:
        self._samples_per_prompt = samples_per_prompt
        self.model = model
        self.prompt_count = 0
        self.log_path = log_path

    def _draw_sample(self, prompt: str) -> str:
        """Returns a predicted continuation of `prompt`."""
        response = self.model.prompt(prompt)
        self._log(prompt, response, self.prompt_count)
        self.prompt_count += 1
        return response

    def draw_samples(self, prompt: str) -> Collection[str]:
        """Returns multiple predicted continuations of `prompt`."""
        return [self._draw_sample(prompt) for _ in range(self._samples_per_prompt)]

    def _log(self, prompt: str, response: str, index: int):
        if self.log_path is not None:
            with open(self.log_path / f"prompt_{index}.log", "a") as f:
                f.write(prompt)
            with open(self.log_path / f"response_{index}.log", "a") as f:
                f.write(str(response))


class vLLM:
    def __init__(
        self, samples_per_prompt: int, url: str, model: str, log_path=None
    ):
        current_dir = Path(__file__).parent
        dotenv_path = current_dir / '.env'
        if dotenv_path.exists():
            load_dotenv(dotenv_path)
        self.key = os.getenv('API_KEY', '') 
        if not self.key:
            raise ValueError("API key not found. Please set the OPENAI_API_KEY environment variable.")
        self.client = OpenAI(api_key=self.key, base_url=url)
        self.samples_per_prompt = samples_per_prompt
        self.log_path = log_path
        self.model = model
        self.prompt_count = 0
        sys_prompt_path = current_dir / "systemprompt.txt"
        if sys_prompt_path.exists():
            with open(sys_prompt_path, "r") as f:
                self.system_prompt = f.read().strip()
        else:
            raise FileNotFoundError(f"System prompt file not found at {sys_prompt_path}")
        
    def draw_samples(self, prompt: str) -> list[ProgramImplementation | None]:
        """Batch processes multiple prompts at once."""
        responses = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            response_format=ProgramImplementation,
            n=self.samples_per_prompt,
        )
        response_msgs = [choice.message.parsed for choice in responses.choices]
        for msg in response_msgs:
            self._log(prompt, msg)
        return response_msgs

    def _log(self, prompt: str, response: str):
        self.prompt_count += 1

        if self.log_path is not None:
            with open(self.log_path / f"prompt_{self.prompt_count}.log", "a") as f:
                f.write(prompt)
            with open(self.log_path / f"response_{self.prompt_count}.log", "a") as f:
                f.write(str(response))


class Sampler:
    """Node that samples program continuations and sends them for analysis."""

    def __init__(
        self,
        database: programs_database.ProgramsDatabase,
        evaluators: Sequence[evaluator.AsyncEvaluator],
        model: LLM | vLLM,
        uid: int = 0,
    ) -> None:
        self._database = database
        self._evaluators = evaluators
        self._llm = model
        self.uid = uid
        self.generation_number = 0

    def sample(self):
        """Continuously gets prompts, samples programs, sends them for analysis."""
        # Time getting prompt
        t0 = time.time()
        prompt = self._database.get_prompt()
        t1 = time.time()
        prompt_time = t1 - t0

        # Time LLM sampling
        t0 = time.time()
        samples = self._llm.draw_samples(prompt.code)
        t1 = time.time()
        llm_time = t1 - t0

        # Time evaluation
        eval_times = []
        for sample in samples:
            curr_id = str(self.uid) + "_" + str(self.generation_number)
            self.generation_number += 1
            t0 = time.time()
            chosen_evaluator = np.random.choice(self._evaluators)
            chosen_evaluator.analyse(sample, prompt.island_id, curr_id)
            t1 = time.time()
            eval_times.append(t1 - t0)

        # Log timing results
        avg_eval_time = sum(eval_times) / len(eval_times)

if __name__ == "__main__":
    from pathlib import Path
    import logging
    from openevolve import extractor, project_indexer
    import pathlib

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.info("Sampler module loaded successfully.")

    model = vLLM(
        samples_per_prompt=2,
        url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-2.0-flash-lite",
        log_path=Path("logs") 
    )

    eval_path = pathlib.Path("examples/astropy_example/repo/astropy/eval.py")
    args = [0]
    base_dir = pathlib.Path("/Users/jameshou/Documents/Code/openevolve/examples/astropy_example/repo/astropy")

    spec_structured, path, program_meta = extractor.extract_code(eval_path, args)

    program = code_manipulation.structured_output_to_prog_meta(spec_structured, program_meta)
    file_hierarchy = project_indexer.ProjectIndexer.get_tree_description(program, base_dir)

    def build_prompt_from_spec_structured(program, file_hierarchy: str = "# File Hierarchy\n") -> str:
        prompt = f"{file_hierarchy}\n"
        prompt += "# Start of Program Version 0 (*_v0)\n"
        for function in program.functions:
            func_loc_comment = f"#{pathlib.Path(function.path).relative_to(base_dir)}: {function.qualname}\n"
            prompt += f"{func_loc_comment}\n"
            prompt += f"{function.to_str(version=0)}\n\n"
        prompt += "# End of Program Version 0\n\n"
        return prompt

    prompt = build_prompt_from_spec_structured(program, file_hierarchy)

    logger.info("Prompt built successfully.")

    samples = model.draw_samples(prompt)

    import pickle

    with open("samples.pkl", "wb") as f:
        pickle.dump(samples, f)