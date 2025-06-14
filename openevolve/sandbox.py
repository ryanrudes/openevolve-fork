from openevolve.test_case import TestCase

from openevolve.constants import (
    HOTSWAP_ENVVAR,
    SANDBOX_IMAGE_NAME,
    SANDBOX_CONTAINER_NAME,
    CONTAINER_MAIN_PATH,
    CONTAINER_EVAL_PATH,
    CONTAINER_LOGS_PATH,
    CONTAINER_INPUTS_PATH,
    CONTAINER_OUTPUTS_PATH,
    CONTAINER_IMPS_PATH,
    CONTAINER_PYTHONPATH,
)

from openevolve.custom_types import (
    HostAbsPath,
    HostRelPath,
    ContainerAbsPath,
)

from openevolve.eval_result import EvalResult

from enum import StrEnum
from typing import Any

import cloudpickle as pickle
import tempfile
import logging
import pathlib
import shutil
import sys
import os

class DummySandbox:
    """Base class for Sandboxes that execute the generated code."""
    sandboxes = 0

    def __init__(self, **kwargs):
        self.sandbox_id = self.register_sandbox()

    def register_sandbox(self) -> int:
        """Register this sandbox in the global list of sandboxes."""
        sandbox_id = DummySandbox.sandboxes
        DummySandbox.sandboxes += 1
        return sandbox_id

class ContainerEngine(StrEnum):
    """Enum for container engines."""
    PODMAN = "podman"
    DOCKER = "docker"

DEFAULT_CONTAINER_ENGINE = ContainerEngine.DOCKER

class ContainerSandbox(DummySandbox):
    """
    Basic sandbox that runs unsafe code in Podman or Docker container.
    - the sandbox should be safe against inadvertent bad code by LLM but not against malicious attacks.
    - does not require any other dependencies on the host than Podman/Docker
    - does not support multithreading
    - might provide easier or more lightweight debugging experience than some other fancier sandbox environments
    """
    engine: ContainerEngine = DEFAULT_CONTAINER_ENGINE

    def __init__(
        self,
        project_root: HostAbsPath,
        imps_root: HostAbsPath,
        eval_relpath: HostRelPath,
        setup_relpath: HostRelPath | None = None,
        force_rebuild_container: bool = False,
        pythonpath: ContainerAbsPath = CONTAINER_PYTHONPATH,
    ):
        """
        Initializes the container sandbox.

        Args:
            project_root (HostAbsPath): The absolute path to the project root on the host.
            imps_root (HostAbsPath): The absolute path to the implementations directory on the host (e.g. /tmp/...).
            eval_relpath (HostRelPath): The path to the evaluation entry point on the host, relative to the project root (e.g. "./eval.py").
            setup_relpath (HostRelPath | None): The path to the setup file for installation on the host, relative to the project root (e.g. "./setup.sh"). If provided, must be a shell script.
            force_rebuild_container (bool): If True, forces the rebuild of the container, even if it already exists.
            pythonpath (ContainerAbsPath): The absolute path to the Python interpreter to use on the host. Defaults to `CONTAINER_PYTHONPATH`.
        """
        super().__init__()

        self.project_root = project_root
        self.imps_root = imps_root
        self.eval_relpath = eval_relpath
        self.setup_relpath = setup_relpath
        self.pythonpath = pythonpath

        eval_abspath = project_root / eval_relpath
        setup_abspath = project_root / setup_relpath if setup_relpath else None

        # Check if setup_file is a shell script
        if setup_abspath is not None and (not setup_abspath.is_file() or setup_abspath.suffix != ".sh"):
            raise ValueError(
                f"setup_file must be a shell script, got {setup_abspath.suffix}"
            )

        # Check that the evaluation entry point is a Python file
        if not eval_abspath.is_file() or eval_abspath.suffix != ".py":
            raise ValueError(
                f"eval_file must be a Python file, got {eval_abspath.suffix}"
            )

        if not eval_abspath.exists():
            raise FileNotFoundError(
                f"Evaluation file {eval_relpath} does not exist in the project root {project_root}."
            )

        if self.sandbox_id != 0:
            return

        # Check if the container image already exists
        # If it does, we can skip the build step unless forced to rebuild
        if force_rebuild_container or not self.container_exists():
            # If the container already exists, remove it
            self.remove_container()

            # Build the container image
            self.build_image(
                project_root,
                eval_relpath,
                setup_relpath,
            )

            # Create the container from the built image
            self.create_container(imps_root)

        # Start the container if it is not already running
        self.start_container()

    @staticmethod
    def has_engine(engine: ContainerEngine) -> bool:
        """Checks if the specified container engine is available."""
        ret = os.system(f"{engine.value} --version")
        return ret == 0

    @classmethod
    def use_engine(cls, engine: ContainerEngine):
        """Sets the container engine to the specified one."""
        logging.debug(f"Using container engine: {engine.value}")
        cls.engine = engine

    @classmethod
    @property
    def executable(cls) -> str:
        """Returns the executable command for the container engine."""
        return cls.engine.value

    @classmethod
    def select_engine(cls) -> ContainerEngine:
        """Selects the container engine to use."""
        if cls.has_engine(ContainerEngine.DOCKER):
            engine = ContainerEngine.DOCKER
        elif cls.has_engine(ContainerEngine.PODMAN):
            engine = ContainerEngine.PODMAN
        else:
            raise Exception("Could not find Podman or Docker. Cannot create sandbox.")

        cls.use_engine(engine)
        return engine

    @classmethod
    def build_image(
        cls,
        project_root: HostAbsPath,
        eval_relpath: HostRelPath,
        setup_relpath: HostAbsPath | None = None,
    ):
        """
        Builds the container image.
        
        Args:
            project_root (HostAbsPath): The absolute path to the project root on the host.
            eval_relpath (HostRelPath): The path to the evaluation entry point on the host, relative to the project root (e.g. "./eval.py").
            setup_relpath (HostAbsPath | None): The absolute path to the setup file for installation, located on the host. If provided, must be a shell script.
        """
        version = sys.version.split(" ")[0]
        logging.debug(f"Using Python version: {version}")

        # Select which container engine to use
        cls.select_engine()

        # Define the path to the Dockerfile
        dockerfile = pathlib.Path(__file__).parent / "container" / "Dockerfile"
        logging.debug("Building container image")

        # Add the setup file as a build argument if provided
        extra = ""

        if setup_relpath is not None:
            extra = f'--build-arg SETUP_RELPATH="{setup_relpath}"'

        # Set the build context to the project root
        build_context = project_root

        # Prepare the command to build the container image
        cmd = (
            # Use the container engine to build the image
            f"{cls.executable} build "
            # Set the build argument for the Python version
            f"--build-arg PYTHON_VERSION={version} "
            # Set the build argument for the workspace root
            f"--build-arg PROJECT_ROOT=. "
            # Set the build argument for the evaluation entry point
            f"--build-arg EVAL_RELPATH={eval_relpath} "
            # Tag the image with the name
            f"-t {SANDBOX_IMAGE_NAME} "
            # Use the Dockerfile from the container directory
            f"-f {dockerfile} {build_context} "
            # Add any extra build arguments, such as the setup file
            f"{extra}"
        )

        # Execute the command to build the image
        logging.debug(f"Executing: {cmd}")
        ret = os.system(cmd)

        if ret != 0:
            raise RuntimeError(
                f"Failed to build the container image {SANDBOX_IMAGE_NAME}. "
                "Please check the Dockerfile and the build context."
            )

    @classmethod
    def create_container(cls, imps_root: HostAbsPath):
        """
        Creates a container from the built image.
        
        Args:
            imps_root (HostAbsPath): The absolute path to the implementations directory on the host (e.g. /tmp/...).
        """
        # Create the container
        logging.debug("Creating container from the built image...")
        
        cmd = (
            f"{cls.executable} create "
            # Set the container to run in interactive mode
            f"-i "
            # Set the container name
            f"--name {SANDBOX_CONTAINER_NAME} "
            # Mount the implementations directory from the host to /implementations in the container
            f"--mount type=bind,source={imps_root},target={CONTAINER_IMPS_PATH},readonly "
            # Use the built image
            f"{SANDBOX_IMAGE_NAME}:latest"
        )

        logging.debug(f"Executing: {cmd}")
        os.system(cmd)

        # Complete the image build process
        logging.debug("Container image built successfully.")

    @classmethod
    def upload_test_cases(cls, test_cases: list[TestCase]):
        """
        Uploads test cases to the container sandbox.

        Args:
            test_cases (list[TestCase]): List of test cases to upload.
        """
        # Create a temporary directory on the host file system
        # to store the test cases.
        temp_dir = tempfile.mkdtemp()

        try:
            # Copy the test case files to the temporary directory
            for i, test_case in enumerate(test_cases):
                # Create a unique filename for each test case
                test_case_file = os.path.join(temp_dir, f"{i}.pickle")

                # Write the test case to a file using cloudpickle
                with open(test_case_file, "wb") as file:
                    test_case_dict = {
                        "args": test_case.args,
                        "kwargs": test_case.kwargs,
                    }

                    pickle.dump(test_case_dict, file)

            # Use the container engine to copy files to the container
            cmd = (
                f"{cls.executable} "
                f"cp {temp_dir}/. "
                f"{SANDBOX_CONTAINER_NAME}:{CONTAINER_INPUTS_PATH}"
            )

            logging.debug(f"Copying test cases to container: {cmd}")
            os.system(cmd)
        except Exception as e:
            logging.error(f"Failed to upload test cases: {e}")
            raise
        finally:
            # Remove the temporary directory after copying
            shutil.rmtree(temp_dir)

    @classmethod
    def image_exists(cls) -> bool:
        """
        Checks if the container image exists.

        Returns:
            bool: True if the image exists, False otherwise.
        """
        cmd = f"{cls.executable} image ls {SANDBOX_IMAGE_NAME} -q"
        result = os.popen(cmd).read().strip()
        return bool(result)

    @classmethod
    def container_exists(cls) -> bool:
        """
        Checks if the container exists.

        Returns:
            bool: True if the container exists, False otherwise.
        """
        cmd = f"{cls.executable} container ls"
        result = os.popen(cmd).read().strip()
        return SANDBOX_CONTAINER_NAME in result

    @classmethod
    def remove_container(cls):
        """
        Removes the container if it exists.
        """
        if cls.container_exists():
            cmd = f"{cls.executable} rm -f {SANDBOX_CONTAINER_NAME}"
            logging.debug(f"Removing container: {cmd}")
            os.system(cmd)
        else:
            logging.debug("No container to remove.")

    @classmethod
    def start_container(cls):
        """
        Starts the container if it is not already running.
        If the container is already running, it will not do anything.
        """
        cmd = (
            f"{cls.executable} start "
            f"{SANDBOX_CONTAINER_NAME}"
        )

        logging.debug(f"Executing: {cmd}")
        os.system(cmd)

    def execute(
        self,
        implementation_id: str,
        test_id: int,
        timeout: float = 30.0,
    ) -> tuple[ContainerAbsPath, int]:
        """
        Use podman/docker to execute python in a container.
        - The main.py shall execute the LLM generated method from prog.pickle file providing
          input.pickle as the input for the method.
        - main.py writes the output of the method into output.pickle.
        Everything except the /workspace folder will be read-only so that the environment remains good
        for future runs.

        Returns:
            Path: The absolute path of the output file in the container.
            int: The exit code of the command executed in the container.
        """
        inputs_filename = f"{test_id}.pickle"
        outputs_filename = f"{implementation_id}/output_{test_id}.pickle"

        inputs_filepath = CONTAINER_INPUTS_PATH / inputs_filename
        outputs_filepath = CONTAINER_OUTPUTS_PATH / outputs_filename

        # Create the directory to store logs for this implementation and test case
        log_dir = CONTAINER_LOGS_PATH / implementation_id / f"test_{test_id}"

        cmd = (
            # Use the container engine to execute the command
            f"{self.executable} exec "
            # Create the log directory and all necessary parent directories
            f"{SANDBOX_CONTAINER_NAME} mkdir -p {log_dir}"
        )
        logging.debug(f"Creating log directory: {cmd}")
        os.system(cmd)

        stdout_path = log_dir / "stdout.txt"
        stderr_path = log_dir / "stderr.txt"

        cmd = (
            # Use the container engine to run the command
            f"{self.executable} exec "
            # Set the container to run on
            f"{SANDBOX_CONTAINER_NAME} "
            # Run the command in a bash shell
            f"/bin/bash -c '"
            # Set the environment variable for hot-swapping
            f"{HOTSWAP_ENVVAR}={implementation_id} "
            # Call the Python interpreter in the container
            f"{self.pythonpath} "
            # Execute the main Python script in the container
            f"{CONTAINER_MAIN_PATH} "
            # Pass the paths to the eval program, input file, output file, and log directory
            f"{CONTAINER_EVAL_PATH} "
            f"{inputs_filepath} "
            f"{outputs_filepath} "
            # Pass the timeout to the main script
            f"{timeout} "
            # Pipe the standard output to a log file
            f"> {stdout_path} "
            # Pipe the standard error output to a log file
            f"2> {stderr_path} "
            # Close the command string
            f"'"
        )
        logging.debug(f"Executing: {cmd}")
        return outputs_filepath, os.system(cmd)

    def run(
        self,
        implementation_id: str,
        test_id: int,
        timeout: float = 30.0,
    ) -> EvalResult:
        """
        Runs the container sandbox with the specified entry point and implementation ID.

        Args:
            implementation_id (str): The ID of the implementation to run.
            test_id (int): The ID of the test case to execute.
            timeout (float): The maximum time in seconds to allow for the function execution.

        Returns:
            EvalResult: The result of the evaluation, containing the output data and exit code.
        """
        output_filepath, retcode = self.execute(implementation_id, test_id, timeout)

        # Create a temporary file to store the output
        output_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pickle")
        
        # Copy the output file from the container to the host
        cmd = (
            f"{self.executable} cp "
            f"{SANDBOX_CONTAINER_NAME}:{output_filepath} "
            f"{output_file.name}"
        )
        logging.debug(f"Copying output file from container: {cmd}")
        os.system(cmd)

        # Load the output data from the temporary file
        with open(output_file.name, "rb") as file:
            eval_result = pickle.load(file)

        # Clean up the temporary file
        os.remove(output_file.name)

        return eval_result

'''
def main(workspace_root, implementations_root):
    ImplementationsManager.set_workspace_root(workspace_root)
    ImplementationsManager.set_implementations_root(implementations_root)
    ImplementationsManager.set_program_meta(program_meta)

    implementation_manager = ImplementationsManager()
    implementation_manager.save_implementation(initial_program, "random")

    # Example usage of the ContainerSandbox
    sandbox = ContainerSandbox(
        workspace_path=pathlib.Path("./astropy"),
        eval_file=pathlib.Path("./astropy/eval.py"),
        implementations_path=implementations_root,
        setup_file=pathlib.Path("./examples/astropy_example/setup.sh")
    )

    sandbox.upload_test_cases(test_cases)

    sandbox.execute("random", 0)

if __name__ == "__main__":
    # Set log level to DEBUG for detailed output
    logging.basicConfig(level=logging.DEBUG)

    import openevolve.extractor as extractor
    from openevolve.evaluator2 import ImplementationsManager
    from pathlib import Path

    workspace_root = Path("/Users/ryanrudes/openevolve/astropy")
    implementations_root = pathlib.Path("/Users/ryanrudes/openevolve/implementations")

    test_cases = [
        TestCase(args=[], kwargs={}),
        TestCase(args=[], kwargs={}),
    ]

    """
    initial_program, evolve_path, program_meta = extractor.extract_code(Path("/Users/ryanrudes/openevolve/astropy/eval.py"), test_cases)

    with open("initial_program.pickle", "wb") as f:
        pickle.dump(initial_program, f)

    with open("program_meta.pickle", "wb") as f:
        pickle.dump(program_meta, f)

    exit()
    """

    with open("initial_program.pickle", "rb") as f:
        initial_program = pickle.load(f)

    with open("program_meta.pickle", "rb") as f:
        program_meta = pickle.load(f)

    extractor.add_decorators(program_meta)

    try:
        main(workspace_root, implementations_root)
    except:
        pass
    finally:
        extractor.remove_decorators(program_meta)
'''