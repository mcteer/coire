import sys

if sys.argv[1:] == ["--healthcheck"]:
    from coire_upgrade_executor.healthcheck import check

    raise SystemExit(check())

from coire_upgrade_executor.main import main

raise SystemExit(main())
