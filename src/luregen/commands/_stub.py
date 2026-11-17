import typer


def not_implemented(feature: str) -> None:
    typer.echo(f"Not implemented yet, see {feature}.", err=True)
    raise typer.Exit(code=2)
