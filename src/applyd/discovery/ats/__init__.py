from . import ashby, greenhouse, lever, smartrecruiters, workable

ATS_MODULES = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
}

__all__ = [
    "ATS_MODULES",
    "ashby",
    "greenhouse",
    "lever",
    "smartrecruiters",
    "workable",
]
