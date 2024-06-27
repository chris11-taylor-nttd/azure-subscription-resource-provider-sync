from enum import StrEnum
import os
import sys
from uuid import UUID

from azure.identity import DefaultAzureCredential, AzureAuthorityHosts
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.subscription.models import Subscription

Registrations = dict[str, bool]

if os.environ.get("ARM_ENVIRONMENT") == "usgovernment":
    authority = AzureAuthorityHosts.AZURE_GOVERNMENT
    resource_manager = "https://management.usgovcloudapi.net"
else:
    authority = AzureAuthorityHosts.AZURE_PUBLIC_CLOUD
    resource_manager = "https://management.azure.com"


CREDENTIAL = DefaultAzureCredential(authority=authority)

subscription_client = SubscriptionClient(
    credential=CREDENTIAL,
    base_url=resource_manager,
    credential_scopes=[resource_manager + "/.default"],
)


class ReplicationStrategy(StrEnum):
    """Replication strategies for determining the action taken on the destination subscription.

    An ECHO operation will ensure that any providers enabled on the source will be enabled on the destination.
    No destination providers will be disabled as part of this process.

    A SYNC operation will ensure that providers on the destination match exactly with those on the source, that
    is, if the source has a provider disabled, it will be disabled on the destination.

    Implementation of these strategies occurs in generate_registration_delta.
    """

    ECHO = "echo"
    SYNC = "sync"


DEFAULT_REPLICATION_STRATEGY = ReplicationStrategy.ECHO


def subscription_id_valid(subscription_id: str) -> bool:
    """Check a subscription_id to determine whether or not it is in a valid format (UUID4).

    Args:
        subscription_id (str): Subscription ID from user

    Returns:
        bool: True if a valid UUID4 is passed, False otherwise.
    """
    try:
        UUID(hex=subscription_id, version=4)
        return True
    except:
        return False


def get_subscription(subscription_id: str) -> Subscription:
    """Returns a single Subscription object from the Azure API.

    Args:
        subscription_id (str): ID of the subscription

    Returns:
        Subscription: Subscription object from Azure API
    """
    return subscription_client.subscriptions.get(subscription_id=subscription_id)


def get_subscription_registrations(subscription: Subscription) -> Registrations:
    """Retrieves the set of provider registration namespaces and the current status for that
    namespace, where a registered service returns True, and an unregistered service returns
    False. Services that are in a transitory state (either Registering or Unregistering)
    reflect their initial state, not desired state.

    Args:
        subscription (Subscription): Azure Subscription object

    Returns:
        Registrations: Mapping of registration namespace to current registration status
    """
    resource_client = ResourceManagementClient(
        subscription_id=subscription.subscription_id,
        credential=CREDENTIAL,
        base_url=resource_manager,
        credential_scopes=[resource_manager + "/.default"],
    )
    all_providers = [p for p in resource_client.providers.list()]
    return {
        p.namespace: True if p.registration_state == "Registered" else False
        for p in all_providers
    }


def set_subscription_registration(
    subscription: Subscription, namespace: str, value: bool
):
    """Adjusts the registration status of a single namespace within a subscription.

    Args:
        subscription (Subscription): Azure Subscription object
        namespace (str): Namespace of the resource provider
        value (bool): True to register the provider, False to unregister the provider
    """
    resource_client = ResourceManagementClient(
        subscription_id=subscription.subscription_id,
        credential=CREDENTIAL,
        base_url=resource_manager,
        credential_scopes=[resource_manager + "/.default"],
    )
    if value:
        resource_client.providers.register(resource_provider_namespace=namespace)
        print(
            f"Registering {namespace} in {subscription.subscription_id} ({subscription.display_name})"
        )
    else:
        resource_client.providers.unregister(resource_provider_namespace=namespace)
        print(
            f"Unregistering {namespace} in {subscription.subscription_id} ({subscription.display_name})"
        )


def generate_registration_delta(
    source_registrations: Registrations,
    destination_registrations: Registrations,
    strategy: ReplicationStrategy,
) -> Registrations:
    """Generates a delta between two subscriptions given a particular strategy.

    Args:
        source_registrations (Registrations): A mapping of provider namespace to registration state to use as the desired state
        destination_registrations (Registrations): A mapping of provider namespace to registration state to use as the current state
        strategy (ReplicationStrategy): A strategy enum that defines the method of producing the delta between the two states

    Raises:
        NotImplementedError: Raised when an unrecognized ReplicationStrategy is passed.

    Returns:
        Registrations: Mapping of provider namespace to desired registration state.
    """
    registration_delta = {}
    match strategy:
        case ReplicationStrategy.ECHO:
            for provider, enabled in source_registrations.items():
                if (
                    enabled
                    and provider in destination_registrations
                    and not destination_registrations[provider]
                ):
                    registration_delta[provider] = True
        case ReplicationStrategy.SYNC:
            for provider, enabled in source_registrations.items():
                if (
                    provider in destination_registrations
                    and not source_registrations[provider]
                    == destination_registrations[provider]
                ):
                    registration_delta[provider] = enabled
        case _:
            raise NotImplementedError(f"Unrecognized replication strategy {strategy}!")
    return registration_delta


def delta_report(registration_delta: Registrations):
    """Produces a pretty version of the delta in registrations between two subscriptions and prints to stdout.

    Args:
        registration_delta (Registrations): Mapping of provider namespace to desired registration state.
    """
    namespace_width = (
        len(
            list(
                sorted(
                    [k for k in registration_delta], key=lambda x: len(x), reverse=True
                )
            )[0]
        )
        + 5
    )
    for k, v in registration_delta.items():
        print(f"{k.ljust(namespace_width)} => {'Register' if v else 'Unregister'}")


def apply_delta(target_subscription: Subscription, registrations: Registrations):
    """Applies a registration delta to a target subscription.

    Args:
        target_subscription (Subscription): Azure subscription object that should be updated
        registrations (Registrations): Mapping of provider namespace to desired registration state.
    """
    for namespace, value in registrations.items():
        set_subscription_registration(
            subscription=target_subscription, namespace=namespace, value=value
        )


def replicate_registrations(
    source: Subscription, destination: Subscription, strategy: ReplicationStrategy
):
    """Provided two Subscriptions and a ReplicationStrategy, this manages the replication of registrations,
    including a confirmation prompt.

    Args:
        source (Subscription): Subscription to use as the desired state
        destination (Subscription): Subscription to modify
        strategy (ReplicationStrategy): Strategy to determine what changes need to be made to the destination.
    """
    source_registrations = get_subscription_registrations(subscription=source)
    destination_registrations = get_subscription_registrations(subscription=destination)
    delta = generate_registration_delta(
        source_registrations=source_registrations,
        destination_registrations=destination_registrations,
        strategy=strategy,
    )
    if not delta:
        print("No delta found between subscriptions.")
        return
    print(
        f"The following changes will be made to subscription {destination.subscription_id} ({destination.display_name}):"
    )
    delta_report(delta)
    confirm = input("Enter 'yes' to proceed: ")
    if confirm.lower().strip() != "yes":
        print("Aborting!")
        return
    apply_delta(target_subscription=destination, registrations=delta)
    print(
        "Provider registration edits complete. Providers may take minutes or longer to register, monitor progress through the Azure console."
    )


def main(
    source_subscription_id: str,
    destination_subscription_id: str,
    strategy_name: str = None,
):
    """Entrypoint from CLI. Manages validating inputs and retrieval of Subscription objects for later use.

    Args:
        source_subscription_id (str): UUID4 String identifying an Azure Subscription.
        destination_subscription_id (str): UUID4 String identifying an Azure Subscription.
        strategy_name (str, optional): Name of the replication strategy. Defaults to None, which will choose the default replication strategy (echo).

    Raises:
        RuntimeError: Raised when the provided subscription IDs of the source and destination match.
        RuntimeError: Raised when the source subscription ID isn't a UUID4.
        RuntimeError: Raised when the destination subscription ID isn't a UUID4.
    """
    if source_subscription_id == destination_subscription_id:
        raise RuntimeError("Subscription ID for source and destination must not match!")
    if not subscription_id_valid(source_subscription_id):
        raise RuntimeError("Source subscription ID doesn't appear to be a valid UUID.")
    if not subscription_id_valid(destination_subscription_id):
        raise RuntimeError(
            "Destination subscription ID doesn't appear to be a valid UUID."
        )
    if strategy_name is None:
        strategy = DEFAULT_REPLICATION_STRATEGY
    else:
        strategy = ReplicationStrategy(strategy_name)

    source_subscription = get_subscription(subscription_id=source_subscription_id)
    destination_subscription = get_subscription(
        subscription_id=destination_subscription_id
    )

    replicate_registrations(
        source=source_subscription,
        destination=destination_subscription,
        strategy=strategy,
    )


def usage():
    print(
        """
sync.py
    Performs updates of Azure Subscriptions' registered providers based on an existing subscription's configuration.
          
Usage:
    sync.py <source_subscription_id> <destination_subscription_id> [strategy]
          
Arguments:
    source_subscription_id          
        ID of a subscription that your user has access to. This subscription will be used to 
        determine what changes to make on the destination subscription.
    destination_subscription_id
        ID of a subscription that your user has access to. This subscription will be updated 
        based on the provider registrations on the source subscription.
    strategy (optional, default: "echo")
        Either "echo" or "sync" depending on how you wish the destination subscription to be updated.
          
Strategies:
    There are two strategies that can be utilized to update the destination. The default strategy is 
    "echo" as it results in the smallest, safest changeset.
          
    echo
        Registrations that are active on the source subscription will be activated on the destination 
        subscription. Registrations that are inactive on the source are not marked inactive on the 
        destination, only new registrations will be made.
    sync
        The state of registrations on the source subscription are replicated exactly to the destination. 
        This option does replicate unregistered providers between subscriptions, so if a provider is 
        active in the destination but inactive in the source, when the "sync" strategy is chosen,
        the provider will be unregistered from the destination.
"""
    )


if __name__ == "__main__":
    if len(sys.argv) == 3:
        strategy = None
    elif len(sys.argv) == 4:
        strategy = sys.argv[3]
    else:
        usage()
        exit(-1)

    source_subscription_id = sys.argv[1]
    destination_subscription_id = sys.argv[2]

    try:
        main(
            source_subscription_id=source_subscription_id,
            destination_subscription_id=destination_subscription_id,
            strategy_name=strategy,
        )
    except Exception as e:
        print(f"Failure: {e}")
        exit(-2)
