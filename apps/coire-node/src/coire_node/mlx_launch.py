"""Invoke the pinned bare mlx.launch entry point with the versioned interpreter."""


def launcher_argv(python: str) -> list[str]:
    # MLX 0.32.2 declares this console entry point but has no mlx.launch module
    # and no __main__ in its implementation module. A fixed entry-point call
    # avoids relocated console-script shebangs without patching native MLX.
    return [python, "-c", "from mlx._distributed_utils.launch import main; main()"]
