from typing import Any, Dict, List, Optional, Tuple
from loguru import logger
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Subset, TensorDataset, ConcatDataset
from tqdm import tqdm
from utils.incremental_data_utils import prepare_data
from models.MemoryReplayBuffer import MemoryReplayBuffer
from models.rolannet import RolanNET
from scripts.test import evaluate
from utils.utils import split_dataset


def train_step(
    model: RolanNET,
    inputs: Subset,
    labels: Subset,
    criterion: nn.Module,
    optimizer: nn.Module,
    total_correct: int,
    total_samples: int,
    batch_count: int,
    running_loss: float,
    classes: Optional[List[int]],
) -> Tuple[int, int, int, int]:

    labels = process_labels(labels)

    model.update_rolann(inputs, labels, classes=classes)
    outputs = model(inputs)

    loss = criterion(outputs, torch.argmax(labels, dim=1))

    if optimizer:
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    pred = torch.argmax(outputs, dim=1)
    labels = torch.argmax(labels, dim=1)
    total_correct += (pred == labels).sum()
    total_samples += labels.size(0)

    running_loss += loss.item()
    batch_count += 1

    return total_correct, total_samples, batch_count, running_loss


def train_ExpansionBuffer(
    model: RolanNET,
    train_dataset: Subset,
    test_dataset: Subset,
    config: Dict[str, Any],
) -> Dict[str, List[float]]:

    logger.remove()  # Remove default logger to customize it
    logger.add(
        lambda msg: print(msg, end=""),
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )

    device = config["device"]
    classes_per_task = config["classes_per_task"]
    samples_per_task = config["samples_per_task"]
    buffer_batch_size = config["buffer_size"]

    criterion = nn.CrossEntropyLoss()
    optimizer = (
        optim.Adam(model.parameters(), lr=config["learning_rate"], weight_decay=1e-5)
        if model.backbone and not config["freeze_mode"] == "all"
        else None
    )

    replayBuffer = MemoryReplayBuffer(
        memory_size_per_class=buffer_batch_size, classes_per_task=classes_per_task
    )

    results: Dict[str, List[float]] = {
        "train_loss": [],
        "train_accuracy": [],
        "test_loss": [],
        "test_accuracy": [],
    }

    task_accuracies: Dict[int, List[float]] = {
        i: [] for i in range(config["num_tasks"])
    }

    task_train_accuracies: Dict[int, float] = {}

    if config["use_wandb"]:
        import wandb

        wandb.init(project="RolanNet-Model", config=config)
        wandb.watch(model)

    for task in range(config["num_tasks"]):

        logger.info(
            f"Training on classes {task*classes_per_task} to {(task+1)*classes_per_task - 1}"
        )

        current_num_classes = (task + 1) * classes_per_task

        class_range = range(task * classes_per_task, (task + 1) * classes_per_task)

        model.rolann.add_num_classes(classes_per_task)

        # Prepare data for current task
        train_subset = prepare_data(
            train_dataset,
            class_range=class_range,
            samples_per_task=samples_per_task,
        )

        if model.backbone and not config["freeze_mode"] == "all":
            global_train_loader, val_loader = split_dataset(
                train_subset=train_subset, config=config
            )
        else:
            global_train_loader = DataLoader(
                train_subset, batch_size=config["batch_size"], shuffle=True
            )

        num_epochs = config["epochs"] if not config["freeze_mode"] == "all" else 1

        best_val_loss = float("inf")
        patience = config["patience"]
        patience_counter = 0

        for epoch in range(num_epochs):

            model.train()

            running_loss = 0.0
            total_correct = 0
            total_samples = 0
            batch_count = 0

            for inputs, labels in tqdm(
                global_train_loader,
                desc=f"Task {task+1} Global Train, Epoch {epoch + 1}",
            ):

                inputs, labels = inputs.to(device), labels.to(device)

                replayBuffer.add_task_samples(inputs, labels, task=task)

                labels = torch.nn.functional.one_hot(
                    labels, num_classes=current_num_classes
                )

                total_correct, total_samples, batch_count, running_loss = train_step(
                    model,
                    inputs,
                    labels,
                    criterion,
                    optimizer,
                    total_correct,
                    total_samples,
                    batch_count,
                    running_loss,
                    classes=None,
                )

            x_memory, y_memory = replayBuffer.get_past_tasks_samples(
                task, batch_size=None
            )

            if x_memory.size(0) > 0:
                past_task_dataset = TensorDataset(x_memory, y_memory)
                local_train_loader = DataLoader(
                    past_task_dataset, batch_size=config["batch_size"], shuffle=True
                )

                for inputs, labels in tqdm(
                    local_train_loader,
                    desc=f"Task {task+1} Local Train, Epoch {epoch + 1}",
                ):

                    inputs, labels = inputs.to(device), labels.to(device)

                    replayBuffer.add_task_samples(inputs, labels, task=task)

                    labels = torch.nn.functional.one_hot(
                        labels, num_classes=current_num_classes
                    )

                    total_correct, total_samples, batch_count, running_loss = (
                        train_step(
                            model,
                            inputs,
                            labels,
                            criterion,
                            optimizer,
                            total_correct,
                            total_samples,
                            batch_count,
                            running_loss,
                            classes=class_range,
                        )
                    )

            epoch_loss = running_loss / batch_count
            epoch_acc = (total_correct / total_samples).item()

            logger.info(
                f"Task {task+1} Epoch {epoch + 1}, Loss: {epoch_loss}, Accuracy: {100 * epoch_acc}"
            )

            task_train_accuracies[task] = epoch_acc

            if model.backbone and not config["freeze_mode"] == "all":
                val_loss, val_accuracy = evaluate(
                    model,
                    val_loader,
                    criterion,
                    device=device,
                    task=task + 1,
                    mode="Validation",
                )

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    patience_counter = 0
                else:
                    patience_counter += 1
                    if patience_counter >= patience:
                        logger.info(f"Early stopping triggered at epoch {epoch + 1}")
                        break

        # Evaluate on all tasks seen so far
        for eval_task in range(task + 1):
            test_subset = prepare_data(
                test_dataset,
                class_range=range(
                    eval_task * classes_per_task, (eval_task + 1) * classes_per_task
                ),
                samples_per_task=config["samples_per_task"],
            )

            test_loader = DataLoader(
                test_subset, batch_size=config["batch_size"], shuffle=True
            )

            test_loss, test_accuracy = evaluate(
                model,
                test_loader,
                criterion,
                device=device,
                task=eval_task + 1,
            )

            task_accuracies[eval_task].append(test_accuracy)

            if (
                eval_task == task and epoch == num_epochs - 1
            ):  # Only log the current task's performance
                if config["use_wandb"]:
                    wandb.log(
                        {
                            f"train_accuracy_task_{task+1}": epoch_acc,
                            f"train_loss_task_{task+1}": epoch_loss,
                            f"test_accuracy_task_{task+1}": test_accuracy,
                            f"test_loss_task_{task+1}": test_loss,
                        }
                    )

                results["train_loss"].append(epoch_loss)
                results["train_accuracy"].append(epoch_acc)
                results["test_loss"].append(test_loss)
                results["test_accuracy"].append(test_accuracy)

    logger.info(
        f"\nMemory buffer distribution: {replayBuffer.get_class_distribution()}"
    )

    if config["use_wandb"]:
        wandb.finish()

    return results, task_train_accuracies, task_accuracies

def process_labels(labels: torch.Tensor) -> torch.Tensor:
    return labels * 0.9 + 0.05