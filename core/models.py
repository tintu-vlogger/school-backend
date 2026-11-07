"""Tables owned by the platform core (audit, masters, custom fields, number series,
workflows). Imported by `modules.import_all_models()` so SQLAlchemy and Alembic see them."""
from core.audit import AuditLog  # noqa: F401
from core.masters import MasterType, MasterValue  # noqa: F401
from core.custom_fields import CustomFieldDef, CustomFieldSection  # noqa: F401
from core.numbering import NumberSeries, NumberSeriesCounter  # noqa: F401
from core.workflow.models import WorkflowDefinition, WorkflowDelegation, WorkflowInstance, WorkflowTask  # noqa: F401
