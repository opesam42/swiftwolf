
class BlacklistedAccount(SQLModel, table=True):
    __tablename__ = "blacklisted_accounts"
    __table_args__ = (
        # create a partial unique index on the beneficiary_account and beneficiary_bank_code columns
        Index(
            "idx_blacklist_active_composite",
            "beneficiary_account",
            "beneficiary_bank_code",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    beneficiary_account: str = Field(max_length=20)
    beneficiary_bank_code: str = Field(max_length=10)
    reason: str = Field(
        max_length=50,
        sa_column_kwargs={"comment": "'confirmed_fraud', 'nibss_watchlist', or 'manual_flag'."},
    )
    beneficiary_name: Optional[str] = Field(
        default=None, max_length=100,
        sa_column_kwargs={"comment": "Resolved account holder name, when known — lets "
                                       "BeneficiaryExportService surface a real name for a "
                                       "blacklisted account too, not just legitimate ones."},
    )
    source: str = Field(
        max_length=50,
        sa_column_kwargs={"comment": "'analyst', 'nibss_sync', or 'layer2_confirmed_fraud'."},
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true"),
                          comment="Soft-delete flag — never hard-delete a blacklist row, "
                                  "the audit trail of who was blacklisted (and when) matters."),
    )
    added_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    added_by: Optional[str] = Field(
        default=None, max_length=64,
        sa_column_kwargs={"comment": "Analyst id, or system name if added automatically."},
    )
    notes: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))