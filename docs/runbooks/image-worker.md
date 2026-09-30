# Studio image worker preparation

The public image submit route remains closed. The node-side image worker load
contract now carries a registry slug, manifest digest, runtime version and
reservation. Before any future mflux launch, `verify_image_copy` checks the
Studio Store copy, its canonical manifest and every file digest. It refuses
symlinks, special files, unsafe asset suffixes, extra files and a runtime other
than `mflux-0.20.0`. It never downloads missing files.

For a load refusal, inspect the admin acquisition and replica manifests on
both Studios using the existing model-copy tools. Repair through the admin
acquisition pipeline and rerun verification; do not edit the copy or its
manifest manually. The native mflux installation is in draft PR #31 and must
land before a live worker can start. To stop image loading, keep
`COIRE_IMAGE_ENABLED=false`; do not remove model copies while an engine is
active. No real Studio or engine was run by this slice.
