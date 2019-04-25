import datetime
import functools
import logging
import pathlib
import os
import shutil

import art.defences
import click
import foolbox
import numpy as np
import sys
import tarfile
import torch
import torchvision

from . import attacks, batch_attack, batch_processing, cifar_models, defenses, detectors, distance_tools, loaders, model_tools, training, rejectors, utils

default_architecture_names = {
    'cifar10': 'densenet (depth=100, growth_rate=12)',
    'cifar100': 'densenet (depth=100, growth_rate=12)',
    'imagenet': 'densenet (depth=161, growth_rate=48)'
}

datasets = ['cifar10', 'cifar100', 'imagenet']
differentiable_attacks = ['deepfool', 'fgsm']
black_box_attacks = ['boundary']
supported_attacks = differentiable_attacks + black_box_attacks

parallelizable_attacks = ['deepfool', 'fgsm']

supported_distance_tools = ['counter-attack']
cache_distance_tools = ['counter-attack']
supported_standard_detectors = []

supported_detectors = supported_distance_tools + supported_standard_detectors

supported_preprocessors = ['feature-squeezing', 'spatial-smoothing']

supported_ps = ['2', 'inf']


logger = logging.getLogger(__name__)


def _get_results_default_path(test_name, dataset, start_time):
    return './results/{}/{} {:%Y-%m-%d %H-%M-%S}.csv'.format(test_name, dataset, start_time)


def get_training_default_path(training_name, dataset, start_time):
    return './trained_models/{}/{} {:%Y-%m-%d %H-%M-%S}.pth.tar'.format(training_name, dataset, start_time)


def get_custom_dataset_default_path(name, original_dataset, start_time):
    return './data/{}/{} {:%Y-%m-%d %H-%M-%S}.zip'.format(name, original_dataset, start_time)

def build_optimiser(optimiser_name, learnable_parameters, options):
    if optimiser_name == 'adam':
        optimiser = torch.optim.Adam(
            learnable_parameters, lr=options['learning_rate'], betas=options['adam_betas'], weight_decay=options['weight_decay'], eps=options['adam_epsilon'], amsgrad=options['adam_amsgrad'])
    elif optimiser_name == 'sgd':
        optimiser = torch.optim.SGD(
            learnable_parameters, lr=options['learning_rate'], momentum=options['sgd_momentum'],
            dampening=options['sgd_dampening'], weight_decay=options['weight_decay'], nesterov=options['sgd_nesterov'])
    else:
        raise ValueError('Optimiser not supported.')

    return optimiser

def _cifar_loader(dataset, path, train, download, batch_size, shuffle, num_workers):
    if dataset == 'cifar10':
        data = torchvision.datasets.CIFAR10
    elif dataset == 'cifar100':
        data = torchvision.datasets.CIFAR100
    else:
        raise ValueError('dataset must be either \'cifar10\' or \'cifar100\'.')

    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        dataset = data(root=path,
                       train=train,
                       download=download,
                       transform=torchvision.transforms.ToTensor())
    except RuntimeError:
        raise RuntimeError(
            'Dataset files not found. Use --download-dataset to automatically download missing files.')
    return torch.utils.data.DataLoader(dataset,
                                       batch_size=batch_size,
                                       shuffle=shuffle,
                                       num_workers=num_workers)


def _download_imagenet(path, config_path):
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)

    train_path = path / 'train'
    train_file_path = train_path / 'ILSVRC2012_img_train.tar'
    val_path = path / 'val'
    val_file_path = val_path / 'ILSVRC2012_img_val.tar'

    train_path.mkdir(parents=True, exist_ok=True)
    utils.download_from_config(
        config_path, train_file_path, 'dataset_links', 'imagenet_train')
    tarfile.open(train_file_path).extractall(train_path)
    os.remove(train_file_path)

    for file_name in os.listdir(train_path):
        logger.debug(file_name)
        # Skip files that are not tar files
        if not file_name.endswith('.tar'):
            continue

        class_file_path = train_path / file_name
        class_path = train_path / file_name[:-4]

        # Create /aaaaa
        os.mkdir(class_path)
        # Extract aaaaa.tar in /aaaaa
        tarfile.open(class_file_path).extractall(class_path)
        # Remove aaaaa.tar
        os.remove(class_file_path)

    val_path.mkdir(parents=True, exist_ok=True)
    utils.download_from_config(
        config_path, val_file_path, 'dataset_links', 'imagenet_val')
    tarfile.open(val_file_path).extractall(val_path)
    os.remove(val_file_path)

    ground_truths = utils.load_json('imagenet_val_ground_truth.txt')[0]
    classes = ground_truths['classes']
    labels = ground_truths['labels']

    for _class in classes:
        os.mkdir(val_path / _class)

    for file_name, label in labels.items():
        shutil.move(val_path / file_name, val_file_path / label)


def _imagenet_loader(path, train, download, batch_size, shuffle, num_workers, config_path):
    if not pathlib.Path(path).exists():
        if download:
            _download_imagenet(path, config_path)
        else:
            raise RuntimeError(
                'Dataset files not found. Use --download-dataset to automatically download missing files.')

    if train:
        data_dir = os.path.join(path, 'train')
    else:
        data_dir = os.path.join(path, 'val')

    transforms = [torchvision.transforms.Resize(256),
                  torchvision.transforms.CenterCrop(224),
                  torchvision.transforms.ToTensor()]

    dataset = torchvision.datasets.ImageFolder(
        data_dir,
        torchvision.transforms.Compose(transforms))

    loader = torch.utils.data.DataLoader(dataset,
                                         batch_size=batch_size,
                                         shuffle=shuffle,
                                         num_workers=num_workers)

    return loader


def _get_genuine_loaders(dataset, path, batch_size, shuffle, num_workers, download, config_path):
    if dataset in ['cifar10', 'cifar100']:
        train_loader = _cifar_loader(
            dataset, path, True, download, batch_size, shuffle, num_workers)
        test_loader = _cifar_loader(
            dataset, path, False, download, batch_size, shuffle, num_workers)

    elif dataset == 'imagenet':
        train_loader = _imagenet_loader(
            path, True, download, batch_size, shuffle, num_workers, config_path)
        test_loader = _imagenet_loader(
            path, False, download, batch_size, shuffle, num_workers, config_path)
    else:
        raise ValueError('Dataset not supported.')

    train_loader = loaders.TorchLoaderWrapper(train_loader)
    test_loader = loaders.TorchLoaderWrapper(test_loader)

    return train_loader, test_loader


def _download_pretrained_model(dataset, path):
    logger.info('Downloading pretrained model.')
    if dataset in ['cifar10', 'cifar100']:
        utils.download_from_config(
            'config.ini', path, 'model_links', dataset)
    elif dataset == 'imagenet':
        model = torchvision.models.densenet161(pretrained=True)

        # We save the model structure, too
        # model_tools.save_state_dict(model, path)
        torch.save(model, path)
    else:
        raise ValueError('Dataset not supported.')


def get_torch_model(dataset: str, is_rejector=False) -> torch.nn.Module:
    """Returns the pretrained Torch model for a given dataset.

    Parameters
    ----------
    dataset : str
        The name of the dataset. Currently supported values
        are ['cifar10', 'cifar100', 'imagenet']
    is_rejector : bool
        If true, it adds an extra class, 'adversarial'

    Raises
    ------
    ValueError
        If the dataset is not supported.

    Returns
    -------
    torch.nn.Module
        The pretrained Torch model for the given dataset.
    """

    if dataset == 'cifar10':
        num_classes = 10
    elif dataset == 'cifar100':
        num_classes = 100
    elif dataset == 'imagenet':
        num_classes = 1000
    else:
        raise ValueError('Dataset not supported')

    if is_rejector:
        num_classes = num_classes + 1

    # Use the models that have shown the best top-1 accuracy
    if dataset in ['cifar10', 'cifar100']:
        # For CIFAR10(0), we use a DenseNet with depth 100 and growth rate 12
        model = cifar_models.densenet(
            depth=100, growthRate=12, num_classes=num_classes)
    elif dataset == 'imagenet':
        # For ImageNet, we use a Densenet with depth 161 and growth rate 48
        model = torchvision.models.densenet161(num_classes=num_classes, pretrained=False)
    else:
        raise ValueError('Dataset not supported.')

    return model


def _get_pretrained_torch_model(dataset: str, base_model: torch.nn.Module, path: str, download: bool) -> torch.nn.Module:
    """Returns the pretrained Torch model for a given dataset.

    Parameters
    ----------
    dataset : str
        The name of the dataset. Currently supported values
        are ['cifar10', 'cifar100', 'imagenet'].
    base_model : torch.nn.Model
        The model on which the pretrained weights will be applied.
    path : str
        The path to the file where the pretrained model is
        stored (or will be downloaded).
    download : bool
        If True it will download the pretrained model if the specified
        file does not exist.

    Raises
    ------
    ValueError
        If the dataset is not supported.

    Returns
    -------
    torch.nn.Module
        The pretrained Torch model for the given dataset.
    """

    # We load the model structure, too

    path = pathlib.Path(path)
    state_dict_path = path.with_name(path.name.split('.')[0] + '_weights' + ''.join(path.suffixes))

    if not path.exists():
        if download:
            path.parent.mkdir(parents=True, exist_ok=True)
            _download_pretrained_model(dataset, str(state_dict_path))
            model_tools.load_state_dict(base_model, state_dict_path, False, False)
            torch.save(base_model, str(path))
        else:
            raise RuntimeError(
                'No pretrained model found: {}. Use --download-model to automatically download missing models.'.format(path))

    model = model_tools.load_state_dict(base_model, str(state_dict_path), False, False)
    # model = torch.load(str(path))
    return model


def _get_normalisation_by_name(dataset: str) -> model_tools.Normalisation:
    """Returns the normalisation for a given dataset.

    Parameters
    ----------
    dataset : str
        The name of the dataset. Currently supported values
        are ['cifar10', 'cifar100', 'imagenet']

    Raises
    ------
    ValueError
        If the dataset is not supported.

    Returns
    -------
    model_tools.Normalisation
        The Normalisation module with the means and standard
        deviations for the given dataset.
    """

    if dataset in ['cifar10', 'cifar100']:
        # The pretrained CIFAR models use the same normalisation
        # for both versions
        means = (0.4914, 0.4822, 0.4465)
        stds = (0.2023, 0.1994, 0.2010)
    elif dataset == 'imagenet':
        means = (0.485, 0.456, 0.406)
        stds = (0.229, 0.224, 0.225)
    else:
        raise ValueError('Dataset not supported.')

    return model_tools.Normalisation(means, stds)

def apply_normalisation(model, normalisation, model_name, option_name):
    has_normalisation = model_tools.has_normalisation(model)

    logger.debug('{} has normalisation: {}'.format(model_name, has_normalisation))

    if not has_normalisation and normalisation is None:
        logger.warning('You are not applying any mean/stdev normalisation to the {}. '
                        'You can specify it by passing {} DATASET '
                        'or {} "RED_MEAN BLUE_MEAN GREEN_MEAN RED_STDEV GREEN_STDEV BLUE_STDEV".'.format(model_name, option_name, option_name))

    if has_normalisation and normalisation is not None:
        logger.warning('You are applying mean/stdev normalisation to the {} multiple times.'.format(model_name))

    if normalisation is not None:
        logger.debug('Applying normalisation for the {}: {}'.format(model_name, normalisation))
        try:
            if normalisation in datasets:
                normalisation_module = _get_normalisation_by_name(normalisation)
            else:
                values = normalisation.split(' ')
                means = float(values[0]), float(
                    values[1]), float(values[2])
                stdevs = float(values[3]), float(
                    values[4]), float(values[5])
                normalisation_module = model_tools.Normalisation(means, stdevs)
        except:
            raise click.BadOptionUsage(option_name, 'Invalid normalisation format for the {}.'.format(model_name))

        model = torch.nn.Sequential(
            normalisation_module, model)

    return model


def _get_num_classes(dataset):
    if dataset == 'cifar10':
        return 10
    elif dataset == 'cifar100':
        return 100
    elif dataset == 'imagenet':
        return 1000
    else:
        raise ValueError('Dataset not supported')


def parse_attack(attack_name, p, foolbox_model, criterion, **attack_call_kwargs):
    attack_constructor = None

    if attack_name == 'deepfool':
        if p == 2:
            attack_constructor = foolbox.attacks.DeepFoolL2Attack
        elif p == np.Infinity:
            attack_constructor = foolbox.attacks.DeepFoolLinfinityAttack
        else:
            raise ValueError('Deepfool supports L-2 and L-Infinity')
    elif attack_name == 'fgsm':
        attack_constructor = foolbox.attacks.FGSM
    elif attack_name == 'boundary':
        attack_constructor = foolbox.attacks.BoundaryAttack
    else:
        raise ValueError('Attack not supported.')

    distance = distance_tools.LpDistance(p)

    attack = attack_constructor(foolbox_model, criterion, distance)

    if len(attack_call_kwargs) > 0:
        logger.debug('Added attack call keyword arguments: {}'.format(
            attack_call_kwargs))
        attack = attacks.AttackWithParameters(attack, **attack_call_kwargs)

    return attack


def parse_distance_tool(tool_name, options, failure_value):
    counter_attack = options['counter_attack']
    defense_p = options['defense_p']
    counter_attack_workers = options['counter_attack_workers']
    foolbox_model = options['foolbox_model']
    counter_attack_parallelization = options['counter_attack_parallelization']
    torch_model = options['torch_model']

    if tool_name == 'counter-attack':
        if counter_attack_parallelization:
            # Note: We use the Torch worker directly (without any defenses) since the counter-attack is the defense
            # We also use it because some attacks require the gradient.

            batch_worker = batch_attack.TorchModelWorker(torch_model)
            thread_worker = batch_attack.AttackWorker(counter_attack, True, foolbox_model)
            parallel_pooler = batch_processing.ParallelPooler(batch_worker, thread_worker, counter_attack_workers)
            
            distance_tool = distance_tools.AdversarialDistance(foolbox_model, counter_attack,
                                                               defense_p, failure_value, parallel_pooler=parallel_pooler)
        else:
            distance_tool = distance_tools.AdversarialDistance(foolbox_model, counter_attack,
                                                               defense_p, failure_value)
    else:
        raise ValueError('Distance tool not supported.')

    return distance_tool


def parse_standard_detector(detector, options, failure_value):
    raise ValueError('Standard detector not supported.')


def parse_preprocessor(preprocessor, options):
    if preprocessor == 'spatial-smoothing':
        return art.defences.SpatialSmoothing(options['spatial_smoothing_window'])
    elif preprocessor == 'feature-squeezing':
        return art.defences.FeatureSqueezing(options['feature_squeezing_bit_depth'])
    else:
        raise ValueError('Preprocessor not supported.')


def add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func
    return _add_options


def set_parameters(parameters):
    def _set_parameters(func):
        @functools.wraps(func)
        def _parse_set_parameters(options, *args, **kwargs):
            set_parameters_options = dict(options)
            for key, value in parameters.items():
                set_parameters_options[key] = value

            return func(set_parameters_options, *args, **kwargs)
        return _parse_set_parameters
    return _set_parameters


def global_options(func):
    @click.argument('dataset', type=click.Choice(datasets))
    @click.option('--batch-size', default=5, show_default=True, type=click.IntRange(1, None),
                  help='The size of each batch.')
    @click.option('--max-model-batch-size', type=click.IntRange(0, None), default=0,
                  help='The maximum number of images passed in the same batch. 0 disables batch limiting (default).')
    @click.option('--max-batches', type=click.IntRange(1, None), default=None,
                  help='The maximum number of batches. If unspecified, no batch limiting is applied.')
    @click.option('--shuffle', type=bool, default=True, show_default=True,
                  help='Whether to shuffle the dataset.')
    @click.option('--config-path', default='./config.ini', type=click.Path(file_okay=True, exists=True),
                  help='The path to the configuration file.')
    @click.option('--no-cuda', is_flag=True)
    @click.option('--no-shuffle-warning', is_flag=True,
                  help='Disables the warning for limiting batches without shuffling.')
    @click.option('--log-level', default='info', show_default=True, type=click.Choice(['debug', 'info', 'warning', 'error', 'critical']),
                  help='Sets the logging level.')
    @functools.wraps(func)
    def _parse_global_options(dataset, batch_size, max_model_batch_size, max_batches, shuffle, config_path, no_cuda, no_shuffle_warning, log_level, *args, **kwargs):
        start_time = datetime.datetime.now()

        command = ' '.join(sys.argv[1:])

        if max_batches is not None:
            if (not shuffle) and (not no_shuffle_warning):
                logger.warning('You are limiting the number of batches, but you aren\'t applying any shuffling. '
                               'This means that the last parts of your dataset will be never used. You can disable this '
                               'warning by passing \'--no-shuffle-warning\'.')

        num_classes = _get_num_classes(dataset)

        logger.debug('CUDA is supported: {}'.format(torch.cuda.is_available()))

        cuda = torch.cuda.is_available() and not no_cuda

        device = torch.cuda.current_device() if cuda else 'cpu'

        logging.getLogger('OffenseDefense').setLevel(log_level.upper())

        global_options = {
            'batch_size': batch_size,
            'command': command,
            'config_path': config_path,
            'cuda': cuda,
            'device': device,
            'dataset': dataset,
            'max_batches': max_batches,
            'max_model_batch_size' : max_model_batch_size,
            'no_shuffle_warning': no_shuffle_warning,
            'num_classes': num_classes,
            'shuffle': shuffle,
            'start_time': start_time
        }

        return func(global_options, *args, **kwargs)
    return _parse_global_options


def standard_model_options(func):
    @functools.wraps(func)
    def _parse_standard_model_options(options, *args, **kwargs):
        dataset = options['dataset']

        base_model = get_torch_model(dataset)

        standard_model_options = dict(options)
        standard_model_options['base_model'] = base_model

        return func(standard_model_options, *args, **kwargs)
    return _parse_standard_model_options


def pretrained_model_options(func):
    """
    Loads the pretrained weights and saves
    the model in foolbox and torch format.

    Requires:
        base_model
        cuda
        dataset
        device
        num_classes

    Adds:
        foolbox_model
        torch_model
    """

    @click.option('--weights-path', type=click.Path(file_okay=True, dir_okay=False), default=None)
    @click.option('--download-model', is_flag=True,
                  help='If the model file does not exist, download the pretrained model for the corresponding dataset.')
    @functools.wraps(func)
    def _parse_pretrained_model_options(options, weights_path, download_model, *args, **kwargs):
        base_model = options['base_model']
        cuda = options['cuda']
        dataset = options['dataset']
        device = options['device']
        max_model_batch_size = options['max_model_batch_size']
        num_classes = options['num_classes']

        if weights_path is None:
            weights_path = './pretrained_models/' + dataset + '.pth.tar'

        logger.debug('Loading pretrained weights from {}'.format(weights_path))

        torch_model = _get_pretrained_torch_model(
            dataset, base_model, weights_path, download_model)

        torch_model = torch.nn.Sequential(
            _get_normalisation_by_name(dataset), torch_model)

        torch_model.eval()

        if cuda:
            torch_model.cuda()

        foolbox_model = foolbox.models.PyTorchModel(
            torch_model, (0, 1), num_classes, channel_axis=3, device=device, preprocessing=(0, 1))

        if max_model_batch_size > 0:
            foolbox_model = model_tools.MaxBatchModel(foolbox_model, max_model_batch_size)

        pretrained_model_options = dict(options)

        pretrained_model_options['foolbox_model'] = foolbox_model
        pretrained_model_options['torch_model'] = torch_model

        return func(pretrained_model_options, *args, **kwargs)
    return _parse_pretrained_model_options


def custom_model_options(func):
    @click.option('--custom-weights-path', type=click.Path(exists=True, file_okay=True, dir_okay=False), default=None)
    @click.option('--custom-model-path', type=click.Path(exists=True, file_okay=True, dir_okay=False), default=None)
    @click.option('--custom-model-normalisation', default=None,
                  help='The normalisation that will be applied by the custom model. Supports both dataset names ({}) and '
                  'channel stds-means (format: "red_mean green_mean blue_mean red_stdev green_stdev blue_stdev" including quotes).'.format(', '.join(datasets)))
    @functools.wraps(func)
    def _parse_custom_model_options(options, custom_weights_path, custom_model_path, custom_model_normalisation, *args, **kwargs):
        cuda = options['cuda']
        dataset = options['dataset']
        device = options['device']
        max_model_batch_size = options['max_model_batch_size']
        num_classes = options['num_classes']

        # NXOR between custom_weights_path and custom_model_path
        if (custom_weights_path is None) == (custom_model_path is None):
            raise click.BadOptionUsage('--custom-weights-path',
                'You must pass either \'--custom-weights-path [PATH]\' or \'--custom-model-path [PATH]\' (but not both).')

        if custom_model_path is None:
            logger.info('No custom architecture path passed. Using default architecture {}'.format(
                default_architecture_names[dataset]))
            custom_torch_model = get_torch_model(dataset)
            logger.debug('Loading weights from {}'.format(custom_weights_path))
            model_tools.load_state_dict(
                custom_torch_model, custom_weights_path, False, False)
        else:
            logger.debug('Loading model from {}'.format(custom_model_path))
            custom_torch_model = torch.load(custom_model_path)

        custom_torch_model = apply_normalisation(custom_torch_model, custom_model_normalisation, 'custom model', '--custom-model-normalisation')

        custom_torch_model.eval()

        if cuda:
            custom_torch_model.cuda()

        custom_foolbox_model = foolbox.models.PyTorchModel(
            custom_torch_model, (0, 1), num_classes, channel_axis=3, device=device, preprocessing=(0, 1))

        if max_model_batch_size > 0:
            logger.debug('Applying model batch limiting: {}'.format(max_model_batch_size))
            custom_foolbox_model = model_tools.MaxBatchModel(custom_foolbox_model, max_model_batch_size)

        custom_model_options = dict(options)
        custom_model_options['custom_foolbox_model'] = custom_foolbox_model
        custom_model_options['custom_torch_model'] = custom_torch_model

        return func(custom_model_options, *args, **kwargs)
    return _parse_custom_model_options


def dataset_options(default_dataset, recommended=None):
    def _dataset_options(func):
        @click.option('--data-folder', default=None, type=click.Path(file_okay=False, dir_okay=True),
                      help='The path to the folder where the dataset is stored (or will be downloaded). '
                      'If unspecified, it defaults to \'./data/genuine/$dataset$\'.')
        @click.option('--dataset-type', default=default_dataset, show_default=True, type=click.Choice(['train', 'test']),
                      help='Sets the dataset (train or test) that will be used.')
        @click.option('--download-dataset', is_flag=True,
                      help='If the dataset files do not exist, download them.')
        @click.option('--loader-workers', default=2, show_default=True, type=click.IntRange(0, None),
                      help='The number of parallel workers that will load the samples from the dataset. '
                      '0 disables parallelization.')
        @functools.wraps(func)
        def _parse_dataset_options(options, data_folder, dataset_type, download_dataset, loader_workers, *args, **kwargs):
            batch_size = options['batch_size']
            config_path = options['config_path']
            dataset = options['dataset']
            max_batches = options['max_batches']
            shuffle = options['shuffle']

            if data_folder is None:
                data_folder = './data/genuine/' + dataset

            if recommended is not None and dataset_type != recommended:
                logger.warning('You are using the {} dataset. We recommend using the {} dataset for this command.'.format(
                    dataset_type, recommended))

            train_loader, test_loader = _get_genuine_loaders(
                dataset, data_folder, batch_size, shuffle, loader_workers, download_dataset, config_path)

            if dataset_type == 'train':
                loader = train_loader
            else:
                loader = test_loader

            if max_batches is not None:
                loader = loaders.MaxBatchLoader(loader, max_batches)

            dataset_options = dict(options)
            dataset_options['dataset_type'] = dataset_type
            dataset_options['loader'] = loader

            return func(dataset_options, *args, **kwargs)
        return _parse_dataset_options
    return _dataset_options


def train_options(func):
    @click.argument('epochs', type=click.IntRange(1, None))
    @click.option('--optimiser', type=click.Choice(['adam', 'sgd']), default='adam', show_default=True)
    @click.option('--learning_rate', type=float, default=1e-3, show_default=True)
    @click.option('--weight-decay', type=float, default=0, show_default=True)
    @click.option('--adam-betas', nargs=2, type=click.Tuple([float, float]), default=(0.9, 0.999), show_default=True)
    @click.option('--adam-epsilon', type=float, default=1e-8, show_default=True)
    @click.option('--adam-amsgrad', is_flag=True)
    @click.option('--sgd-momentum', type=float, default=0, show_default=True)
    @click.option('--sgd-dampening', type=float, default=0, show_default=True)
    @click.option('--sgd-nesterov', is_flag=True)
    @functools.wraps(func)
    def _parse_train_options(options, epochs, optimiser, learning_rate, weight_decay, adam_betas, adam_epsilon, adam_amsgrad, sgd_momentum, sgd_dampening, sgd_nesterov, *args, **kwargs):
        train_options = dict(options)

        train_options['adam_amsgrad'] = adam_amsgrad
        train_options['adam_betas'] = adam_betas
        train_options['adam_epsilon'] = adam_epsilon
        train_options['epochs'] = epochs
        train_options['learning_rate'] = learning_rate
        train_options['optimiser_name'] = optimiser
        train_options['sgd_dampening'] = sgd_dampening
        train_options['sgd_momentum'] = sgd_momentum
        train_options['sgd_nesterov'] = sgd_nesterov
        train_options['weight_decay'] = weight_decay

        return func(train_options, *args, **kwargs)
    return _parse_train_options


def test_options(test_name):
    def _test_options(func):
        @click.option('--results-path', default=None, type=click.Path(file_okay=True, dir_okay=False),
                      help='The path to the CSV file where the results will be saved. If unspecified '
                      'it defaults to \'./results/{}/$dataset$ $start_time$.csv\''.format(test_name))
        @functools.wraps(func)
        def _parse_test_options(options, results_path, *args, **kwargs):
            dataset = options['dataset']
            start_time = options['start_time']

            if results_path is None:
                results_path = _get_results_default_path(
                    test_name, dataset, start_time)

            test_options = dict(options)
            test_options['results_path'] = results_path

            return func(test_options, *args, **kwargs)
        return _parse_test_options
    return _test_options

def attack_options(attacks, mandatory_parallelization=False):
    if mandatory_parallelization:
        _min_attack_workers = 1
        _attack_workers_help = 'The number of parallel workers that will be used to speed up the attack.'
    else:
        _min_attack_workers = 0
        _attack_workers_help = 'The number of parallel workers that will be used to speed up the attack. 0 disables parallelization.'

    def _attack_options(func):
        @click.argument('attack', type=click.Choice(attacks))
        @click.argument('attack_p', type=click.Choice(supported_ps))
        @click.option('--attack-workers', default=0, show_default=True, type=click.IntRange(_min_attack_workers, None),
                  help=_attack_workers_help)
        @functools.wraps(func)
        def _parse_attack_options(options, attack, attack_p, attack_workers, *args, **kwargs):
            attack_p = float(attack_p)

            if attack in parallelizable_attacks:
                logger.debug('Attack supports parallelization.')
            else:
                logger.debug('Attack does not support parallelization.')

                if attack_workers > 0:
                    raise click.BadOptionUsage('--attack-workers', 'The chosen attack \'{}\' does not support parallelization.'.format(attack))

            attack_parallelization = attack_workers > 0

            logger.info('Attack parallelization: {} ({} workers).'.format(attack_parallelization, attack_workers))

            attack_options = dict(options)

            # We don't immediately parse 'attack' because every test needs a specific configuration
            attack_options['attack_name'] = attack
            attack_options['attack_p'] = attack_p
            attack_options['attack_parallelization'] = attack_parallelization
            attack_options['attack_workers'] = attack_workers

            return func(attack_options, *args, **kwargs)
        return _parse_attack_options
    return _attack_options


def distance_tool_options(func):
    @click.argument('defense_p', type=click.Choice(supported_ps))
    @functools.wraps(func)
    def _parse_distance_tool_options(options, defense_p, *args, **kwargs):
        defense_p = float(defense_p)

        distance_tool_options = dict(options)

        distance_tool_options['defense_p'] = defense_p

        return func(distance_tool_options, *args, **kwargs)
    return _parse_distance_tool_options


def counter_attack_options(required):
    def _counter_attack_options(func):
        @click.option('--counter-attack-workers', default=None, type=click.IntRange(0, None),
                      help='The number of attack workers of the counter attack. If unspecified, it defaults to the number of attack workers. 0 disables parallelization.')
        @functools.wraps(func)
        def _parse_counter_attack_options(options, counter_attack, counter_attack_workers, *args, **kwargs):
            attack_workers = options['attack_workers']
            defense_p = options['defense_p']
            foolbox_model = options['foolbox_model']
            max_model_batch_size = options['max_model_batch_size']

            if counter_attack in parallelizable_attacks:
                logger.debug('Counter attack supports parallelization.')
                if counter_attack_workers is None:
                    counter_attack_workers = attack_workers
                    logger.debug('--counter-attack-workers not set, defaulting to --attack-workers ({}).'.format(attack_workers))
            else:
                logger.debug('Counter attack does not support parallelization.')

                if counter_attack_workers is not None and counter_attack_workers > 0:
                    raise click.BadOptionUsage('--counter-attack-workers', 'The chosen counter-attack \'{}\' does not support parallelization.'.format(counter_attack))

                counter_attack_workers = 0

            counter_attack_parallelization = counter_attack_workers > 0
            logger.info('Counter attack parallelization: {} ({} workers).'.format(counter_attack_parallelization, counter_attack_workers))

            if max_model_batch_size > 0 and counter_attack_workers > max_model_batch_size:
                raise click.BadOptionUsage('--counter-attack-workers',
                    'The number of counter attack workers must be at most the maximum model batch size. '
                    'Either increase the maximum model batch size, decrease the number of '
                    'counter attack workers, or disable model batch limiting.')

            counter_attack = parse_attack(
                counter_attack, defense_p, foolbox_model, foolbox.criteria.Misclassification())

            counter_attack_options = dict(options)

            counter_attack_options['counter_attack'] = counter_attack
            counter_attack_options['counter_attack_parallelization'] = counter_attack_parallelization
            counter_attack_options['counter_attack_workers'] = counter_attack_workers

            return func(counter_attack_options, *args, **kwargs)

        parse_func = _parse_counter_attack_options
        if required:
            parse_func = click.argument(
                'counter_attack', type=click.Choice(supported_attacks))(parse_func)
        else:
            parse_func = click.option('--counter-attack', default='deepfool', type=click.Choice(supported_attacks),
                                      help='The counter-attack that will be used (if required).')(parse_func)

        return parse_func
    return _counter_attack_options


def detector_options(func):
    @click.argument('detector', type=click.Choice(supported_detectors))
    @click.option('--reject-on-failure', type=bool, default=True, show_default=True,
                  help='If True, samples for which the detector cannot compute the score will be rejected. If False, they will be accepted.')
    @click.option('--cache-size', type=click.IntRange(0, None), default=0, show_default=True,
                  help='The size of the distance tool cache. 0 disables caching.')
    @functools.wraps(func)
    def _parse_detector_options(options, detector, reject_on_failure, cache_size, *args, **kwargs):

        # To avoid confusion
        detector_name = detector
        del detector

        if reject_on_failure:
            failure_value = -np.Infinity
        else:
            failure_value = np.Infinity

        if detector_name in supported_distance_tools:
            logger.debug('The detector is a distance tool.')
            detector_type = 'distance'

            if cache_size == 0:
                logger.debug('Caching disabled.')
                enable_caching = False
            else:
                if detector_name in cache_distance_tools:
                    logger.debug('Caching enabled')
                    enable_caching = True
                else:
                    logger.debug(
                        'Caching is enabled, but the distance tool does not support it.')
                    enable_caching = False

            distance_tool = parse_distance_tool(
                detector_name, options, failure_value)
            detector = detectors.DistanceDetector(distance_tool)

        elif detector_name in supported_standard_detectors:
            logger.debug('The detector is a standard detector.')
            detector_type = 'standard'

            enable_caching = False
            distance_tool = None
            detector = parse_standard_detector(
                detector_name, options, failure_value)
        else:
            raise ValueError('Detector not supported.')

        detector_options = dict(options)

        detector_options['cache_size'] = cache_size
        detector_options['detector'] = detector
        detector_options['detector_name'] = detector_name
        detector_options['detector_type'] = detector_type
        detector_options['distance_tool'] = distance_tool
        detector_options['enable_caching'] = enable_caching
        detector_options['failure_value'] = failure_value

        return func(detector_options, *args, **kwargs)
    return _parse_detector_options


def rejector_options(func):
    @click.argument('threshold', type=float)
    @functools.wraps(func)
    def _parse_rejector_options(options, threshold, *args, **kwargs):
        cache_size = options['cache_size']
        defense_p = options['defense_p']
        detector = options['detector']
        detector_type = options['detector_type']
        distance_tool = options['distance_tool']
        enable_caching = options['enable_caching']

        if enable_caching:
            assert detector_type == 'distance'
            assert distance_tool is not None

            rejector = rejectors.CacheRejector(
                distance_tool, threshold, defense_p, cache_size)

        else:
            rejector = rejectors.DetectorRejector(detector, threshold)

        rejector_options = dict(options)

        rejector_options['rejector'] = rejector
        rejector_options['threshold'] = threshold

        return func(rejector_options, *args, **kwargs)

    return _parse_rejector_options


def preprocessor_options(func):
    @click.argument('preprocessor', type=click.Choice(supported_preprocessors))
    @click.option('--feature-squeezing-bit-depth', type=int, default=8, show_default=True,
                  help='The bit depth of feature squeezing (only applied if preprocessor is \'feature_squeezing\').')
    @click.option('--spatial-smoothing-window', type=int, default=3, show_default=True,
                  help='The size of the sliding window for spatial smoothing (only applied if preprocessor is \'spatial_smoothing\').')
    @functools.wraps(func)
    def _parse_preprocessor_options(options, preprocessor, feature_squeezing_bit_depth, spatial_smoothing_window, *args, **kwargs):

        preprocessor_options = dict(options)

        preprocessor_options['feature_squeezing_bit_depth'] = feature_squeezing_bit_depth
        preprocessor_options['spatial_smoothing_window'] = spatial_smoothing_window

        # preprocessor must be parsed last
        preprocessor = parse_preprocessor(
            preprocessor, preprocessor_options)
        preprocessor_options['preprocessor'] = preprocessor
        return func(preprocessor_options, *args, **kwargs)

    return _parse_preprocessor_options


def adversarial_dataset_options(func):
    @click.argument('adversarial_dataset_path', type=click.Path(exists=True, file_okay=True, dir_okay=True))
    @click.option('--max-adversarial_batches', type=click.IntRange(1, None), default=None,
                  help='The maximum number of batches. If unspecified, no batch limiting is applied.')
    @functools.wraps(func)
    def _parse_adversarial_dataset_options(options, adversarial_dataset_path, max_adversarial_batches, *args, **kwargs):
        batch_size = options['batch_size']
        shuffle = options['shuffle']

        adversarial_list, adversarial_generation_success_rate = utils.load_zip(
            adversarial_dataset_path)

        adversarial_loader = loaders.ListLoader(
            adversarial_list, batch_size, shuffle)

        if max_adversarial_batches is not None:
            if (not options['shuffle']) and (not options['no_shuffle_warning']):
                logger.warning('You are limiting the number of adversarial batches, but you are not applying any shuffling. '
                               'This means that the last parts of your adversarial dataset will be never used. You can disable this '
                               'warning by passing \'--no-shuffle-warning\'.')

            adversarial_loader = loaders.MaxBatchLoader(
                adversarial_loader, max_adversarial_batches)

        adversarial_dataset_options = dict(options)
        adversarial_dataset_options['adversarial_loader'] = adversarial_loader
        adversarial_dataset_options['adversarial_generation_success_rate'] = adversarial_generation_success_rate

        return func(adversarial_dataset_options, *args, **kwargs)

    return _parse_adversarial_dataset_options

def substitute_options(func):
    @click.argument('substitute_model_path', type=click.Path(exists=True, file_okay=True, dir_okay=False))
    @click.option('--substitute-normalisation', default=None,
                  help='The normalisation that will be applied by the substitute model. Supports both dataset names ({}) and '
                  'channel stds-means (format: "red_mean green_mean blue_mean red_stdev green_stdev blue_stdev" including quotes).'.format(', '.join(datasets)))
    @functools.wraps(func)
    def _parse_substitute_options(options, substitute_model_path, substitute_normalisation, *args, **kwargs):
        cuda = options['cuda']
        device = options['device']
        max_model_batch_size = options['max_model_batch_size']
        num_classes = options['num_classes']

        substitute_torch_model = torch.load(substitute_model_path)

        substitute_torch_model = apply_normalisation(substitute_torch_model, substitute_normalisation, 'substitute model', '--substitute-normalisation')

        substitute_torch_model.eval()

        if cuda:
            substitute_torch_model.cuda()

        substitute_foolbox_model = foolbox.models.PyTorchModel(
            substitute_torch_model, (0, 1), num_classes, channel_axis=3, device=device, preprocessing=(0, 1))

        if max_model_batch_size > 0:
            substitute_foolbox_model = model_tools.MaxBatchModel(substitute_foolbox_model, max_model_batch_size)

        substitute_options = dict(options)
        substitute_options['substitute_foolbox_model'] = substitute_foolbox_model
        substitute_options['substitute_torch_model'] = substitute_torch_model

        return func(substitute_options, *args, **kwargs)
    
    return _parse_substitute_options

def approximation_dataset_options(defense_name):
    def _approximation_dataset_options(func):
        @click.option('--approximation-dataset-path', type=click.Path(exists=False, file_okay=True, dir_okay=False), default=None)
        @functools.wraps(func)
        def _parse_approximation_dataset_options(options, approximation_dataset_path, *args, **kwargs):
            dataset = options['dataset']
            start_time = options['start_time']

            if approximation_dataset_path is None:
                approximation_dataset_path = get_custom_dataset_default_path('approximation/' + defense_name, dataset, start_time)

            approximation_dataset_options = dict(options)

            approximation_dataset_options['approximation_dataset_path'] = approximation_dataset_path

            return func(approximation_dataset_options, *args, **kwargs)
        return _parse_approximation_dataset_options
    return _approximation_dataset_options
        
